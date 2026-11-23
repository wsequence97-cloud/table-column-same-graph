"""对完整 MCP 快照去重、检查定义冲突，并维护跨进程的轻量统计历史。"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from statistics import median
from tempfile import NamedTemporaryFile

from table_column_same_graph.config import MetadataQualityConfig
from table_column_same_graph.models import MetadataRow


logger = logging.getLogger(__name__)
COUNT_FIELDS = (
    "raw_row_count", "field_count", "table_count", "duplicate_count",
    "empty_table_comment_count", "empty_column_comment_count", "empty_column_type_count",
)


def _snapshot_day(snapshot: str) -> date | None:
    """只从 YYYYMMDD 和 YYYY-MM-DD 提取业务日期，其他标识按原值核对。"""
    if re.fullmatch(r"[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2}", snapshot):
        try:
            return date.fromisoformat(snapshot)
        except ValueError:
            pass
    return None


def _unique_rows(rows: list[MetadataRow], snapshot: str) -> tuple[list[MetadataRow], dict]:
    if not rows:
        raise ValueError(f"MCP 元数据快照 {snapshot!r} 的全量结果为空")
    fields = {}
    tables = {}
    for number, row in enumerate(rows, start=1):
        table = (row.database_name, row.table_name)
        first_comment, first_number = tables.setdefault(table, (row.table_comment, number))
        if row.table_comment != first_comment:
            raise ValueError(f"快照 {snapshot!r} 第 {first_number}、{number} 行的表 {table!r} 定义冲突：table_comment")

        field = (*table, row.column_name)
        first_row, first_number = fields.setdefault(field, (row, number))
        if first_row != row:
            changes = [key for key in ("column_type", "column_comment")
                       if getattr(first_row, key) != getattr(row, key)]
            raise ValueError(f"快照 {snapshot!r} 第 {first_number}、{number} 行的字段 {field!r} 定义冲突：{', '.join(changes)}")

    unique = [row for row, _ in fields.values()]
    counts = {
        "raw_row_count": len(rows),
        "field_count": len(unique),
        "table_count": len(tables),
        "duplicate_count": len(rows) - len(unique),
        "empty_table_comment_count": sum(not row.table_comment for row in unique),
        "empty_column_comment_count": sum(not row.column_comment for row in unique),
        "empty_column_type_count": sum(not row.column_type for row in unique),
    }
    return unique, counts


@contextmanager
def _history_lock(path: Path):
    """锁住独立的文件，避免原子替换数据文件时改变锁的对象。进程退出即释放。"""
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OSError(f"无法取得元数据质量历史锁 {lock_path}，请检查其他写入进程及文件权限") from exc
        yield


def _validate_history_record(record: dict) -> None:
    snapshot = record["snapshot_pt"]
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise ValueError("快照标识无效")
    if any(type(record[k]) is not int or record[k] < 0 for k in COUNT_FIELDS):
        raise ValueError("计数必须是非负整数")
    fields = record["field_count"]
    if not 1 <= record["table_count"] <= fields:
        raise ValueError("表数或字段数无效")
    if record["raw_row_count"] != fields + record["duplicate_count"]:
        raise ValueError("原始行数与去重统计不一致")
    for key in ("empty_table_comment_count", "empty_column_comment_count", "empty_column_type_count"):
        if record[key] > fields:
            raise ValueError(f"{key} 超出字段数")
    if datetime.fromisoformat(record["validated_at"]).tzinfo is None:
        raise ValueError("校验时间缺少时区")


def _read_history(path: Path) -> dict[date | str, dict]:
    try:
        with path.open(encoding="utf-8") as source:
            records = json.load(source)
        if not isinstance(records, list) or not records:
            raise ValueError("预期非空统计记录列表")
        history = {}
        for record in records:
            _validate_history_record(record)
            snapshot = record["snapshot_pt"]
            key = _snapshot_day(snapshot) or snapshot
            if key in history:
                raise ValueError("同一日期或快照标识重复")
            history[key] = record
        return history
    except FileNotFoundError:
        return {}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"元数据质量历史 {path} 损坏，无法建立基线") from exc


def _write_history(path: Path, history: dict) -> None:
    temp_path = None
    try:
        with NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".",
                                suffix=".tmp", encoding="utf-8", delete=False) as target:
            temp_path = Path(target.name)
            json.dump(list(history.values()), target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _check_volume(history: dict, day: date | None, snapshot: str, counts: dict,
                  config: MetadataQualityConfig) -> None:
    dates = sorted(key for key in history if isinstance(key, date) and (day is None or key < day))
    dates = dates[-config.window_size:]
    if len(dates) < config.window_size:
        logger.info("快照 %r %s：已有 %d/%d 个历史日期，尚未启用波动阻断", snapshot,
                    "基线初始化" if not dates else "基线积累中", len(dates), config.window_size)
        return

    drops = []
    for key in ("field_count", "table_count"):
        baseline = median(history[d][key] for d in dates)
        drop = (baseline - counts[key]) / baseline
        if drop > config.max_drop_fraction:
            drops.append(f"{key} 当前 {counts[key]}，历史中位数 {baseline}，下降 {drop:.2%}")
    if drops:
        raise ValueError(f"快照 {snapshot!r} 元数据量下降超过 {config.max_drop_fraction:.2%}：{'；'.join(drops)}")


def validate_snapshot(rows: list[MetadataRow], snapshot: str, config: MetadataQualityConfig) -> list[MetadataRow]:
    unique, counts = _unique_rows(rows, snapshot)
    anomalies = {key: counts[key] for key in COUNT_FIELDS
                 if (key == "duplicate_count" or key.startswith("empty_")) and counts[key]}
    if anomalies:
        logger.warning("快照 %r 元数据质量告警 %s", snapshot, anomalies)

    day = _snapshot_day(snapshot)
    if day is None:
        logger.warning("快照 %r 没有可识别的业务日期，保留统计用于回放核对，不计入日期基线", snapshot)
    key = day or snapshot
    path = config.history_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _history_lock(path):
        history = _read_history(path)
        previous = history.get(key)
        if previous is not None:
            changed = [k for k in counts if previous[k] != counts[k]]
            if changed:
                raise ValueError(f"快照 {snapshot!r} 与已有成功统计不一致：{', '.join(changed)}")
        _check_volume(history, day, snapshot, counts, config)
        if previous is None:
            history[key] = {"snapshot_pt": snapshot, **counts,
                            "validated_at": datetime.now(timezone.utc).isoformat()}
            _write_history(path, history)
    return unique
