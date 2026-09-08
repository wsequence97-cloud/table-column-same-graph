"""CSV 输出工具。"""

import csv
from datetime import datetime
from pathlib import Path
from typing import List

from table_column_same_graph.models import ColumnMatch, GraphState, TableMatch


def write_matches_to_csv(state: GraphState) -> str:
    """按当前意图写出 CSV，并返回输出文件路径。"""

    output_dir = Path(state["output_dir"]).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    intent = state["intent"]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if intent == "single_table":
        source_table = state["source_table"]
        file_stem = source_table.table_name or source_table.table_comment or "原表"
        if state.get("has_ddl"):
            headers = ["原表", "原表中文名", "相似表名", "相似表中文名", "全表字段相似度"]
            rows = _table_field_rows(state.get("table_matches", []))
        else:
            headers = ["原表", "原表中文名", "相似表名", "相似表中文名", "相似度"]
            rows = _table_name_rows(state.get("table_matches", []))
    elif intent == "all_tables":
        file_stem = "全表"
        headers = ["原表", "原表中文名", "相似表名", "相似表中文名", "全表字段相似度"]
        rows = _table_field_rows(state.get("table_matches", []))
    elif intent == "single_column":
        source_column = state["source_column"]
        file_stem = source_column.column_name or source_column.column_comment or "原字段"
        headers = ["原字段名", "原表名", "相似字段名", "相似表名", "相似度"]
        rows = _column_rows(state.get("column_matches", []))
    else:
        file_stem = "全字段"
        headers = ["原字段名", "原表名", "相似字段名", "相似表名", "相似度"]
        rows = _column_rows(state.get("column_matches", []))

    file_name = f"{_safe_file_stem(file_stem)}_{timestamp}_csv.csv"
    output_file = output_dir / file_name
    # utf-8-sig 让 Excel 打开中文 CSV 时不乱码。
    with output_file.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.writer(file_obj)
        writer.writerow(headers)
        writer.writerows(rows)
    return str(output_file)


def _table_name_rows(matches: List[TableMatch]) -> List[List[str]]:
    """表名/表中文名相似结果行。"""

    return [
        [
            match.source_table_name,
            match.source_table_comment,
            match.target_table_name,
            match.target_table_comment,
            _format_score(match.name_similarity),
        ]
        for match in matches
    ]


def _table_field_rows(matches: List[TableMatch]) -> List[List[str]]:
    """表字段集合相似结果行。"""

    return [
        [
            match.source_table_name,
            match.source_table_comment,
            match.target_table_name,
            match.target_table_comment,
            _format_score(match.field_similarity or 0.0),
        ]
        for match in matches
    ]


def _column_rows(matches: List[ColumnMatch]) -> List[List[str]]:
    """字段/指标相似结果行。"""

    return [
        [
            match.source_column_name,
            match.source_table_name,
            match.target_column_name,
            match.target_table_name,
            _format_score(match.similarity),
        ]
        for match in matches
    ]


def _format_score(score: float) -> str:
    """将 0-1 小数转换为百分比字符串。"""

    return f"{score:.2%}"


def _safe_file_stem(value: str) -> str:
    """生成安全文件名前缀。"""

    safe = "".join(char if char.isalnum() or char in {"_", "-", "中", "文"} else "_" for char in value.strip())
    return safe[:80] or "result"
