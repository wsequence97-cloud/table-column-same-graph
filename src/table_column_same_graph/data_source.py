"""元数据数据源读取。

当前支持两种来源：
1. hive：从 zz.dwd_meta_table_columns_detail_df 最新分区读取线上元数据；
2. csv：从本地样例或离线导出的 CSV 读取，便于 demo、单测和无网络环境调试。
"""

import csv
import os
from pathlib import Path
from typing import Iterable, List

from table_column_same_graph.config import HIVE_CONFIG
from table_column_same_graph.models import MetadataRow


def load_metadata_rows(source_type: str = "hive", metadata_csv: str = "") -> List[MetadataRow]:
    """根据数据源类型加载字段粒度元数据。"""

    if source_type == "csv":
        if not metadata_csv:
            raise ValueError("source_type=csv 时必须传入 metadata_csv 路径")
        return load_metadata_from_csv(metadata_csv)
    if source_type == "hive":
        return load_metadata_from_hive()
    raise ValueError(f"不支持的数据源类型：{source_type}，当前支持 hive/csv")


def load_metadata_from_csv(metadata_csv: str) -> List[MetadataRow]:
    """从 CSV 文件读取元数据。

    CSV 字段名兼容 table_coment 和 table_comment 两种拼写，方便直接导出 Hive 元数据后使用。
    """

    path = Path(metadata_csv).expanduser().resolve()
    rows: List[MetadataRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for raw in reader:
            if _is_deleted(raw):
                continue
            rows.append(_row_from_mapping(raw))
    return rows


def load_metadata_from_hive() -> List[MetadataRow]:
    """从 Hive 最新分区读取元数据。

    PyHive 使用 host/port 连接；用户给出的 jdbc:hive2://180.184.31.191:10000
    已在 config 中拆成 host=180.184.31.191、port=10000。
    """

    try:
        from pyhive import hive
    except ImportError as exc:
        raise RuntimeError("缺少 pyhive 依赖，请先安装 requirements.txt") from exc

    host = os.getenv("HIVE_HOST", HIVE_CONFIG.host)
    port = int(os.getenv("HIVE_PORT", str(HIVE_CONFIG.port)))
    username = os.getenv("HIVE_USERNAME") or None
    database = os.getenv("HIVE_DATABASE", HIVE_CONFIG.database)

    # 默认无账号密码连接，符合用户给出的 jdbc 地址说明；如企业环境需要用户名，可用环境变量覆盖。
    connection = hive.connect(host=host, port=port, username=username, database=database)
    try:
        cursor = connection.cursor()
        cursor.execute(_build_metadata_sql())
        return [_row_from_tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _build_metadata_sql() -> str:
    """生成读取最新分区元数据的 Hive SQL。

    table_coment 沿用用户提供的元数据表字段名；如果线上表后续修正为 table_comment，
    可在这里调整字段映射，不影响图节点和匹配算法。
    """

    full_table_name = f"{HIVE_CONFIG.database}.{HIVE_CONFIG.table}"
    partition = HIVE_CONFIG.partition_column
    return f"""
SELECT
  database_name,
  table_name,
  table_coment AS table_comment,
  column_name,
  column_type,
  column_comment
FROM {full_table_name}
WHERE is_del = 0
  AND {partition} = (SELECT MAX({partition}) FROM {full_table_name})
"""


def _row_from_mapping(raw: dict) -> MetadataRow:
    """将 CSV DictReader 的一行转换为 MetadataRow。"""

    return MetadataRow(
        database_name=(raw.get("database_name") or "zz").strip(),
        table_name=(raw.get("table_name") or "").strip(),
        table_comment=(raw.get("table_comment") or raw.get("table_coment") or "").strip(),
        column_name=(raw.get("column_name") or "").strip(),
        column_type=(raw.get("column_type") or "").strip(),
        column_comment=(raw.get("column_comment") or "").strip(),
    )


def _row_from_tuple(raw: Iterable[str]) -> MetadataRow:
    """将 Hive fetchall 返回的 tuple 转换为 MetadataRow。"""

    values = list(raw)
    return MetadataRow(
        database_name=str(values[0] or ""),
        table_name=str(values[1] or ""),
        table_comment=str(values[2] or ""),
        column_name=str(values[3] or ""),
        column_type=str(values[4] or ""),
        column_comment=str(values[5] or ""),
    )


def _is_deleted(raw: dict) -> bool:
    """CSV 离线文件中如果包含 is_del=1，则过滤删除字段。"""

    value = str(raw.get("is_del") or "0").strip()
    return value == "1"
