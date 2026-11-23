import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from table_column_same_graph import data_source
from table_column_same_graph.models import MetadataRow


ROW = dict(database_name="zz", table_name="orders", table_comment="订单",
           column_name="id", column_type="STRING", column_comment="编号")


@pytest.fixture
def history(tmp_path, monkeypatch):
    path = tmp_path / "quality" / "history.json"
    monkeypatch.setenv("METADATA_QUALITY_HISTORY_PATH", str(path))
    return path


@pytest.fixture
def load(monkeypatch):
    def run(rows, snapshot="20261123"):
        fetch = AsyncMock(return_value=(rows, snapshot))
        monkeypatch.setattr(data_source, "fetch_metadata_pages", fetch)
        result = asyncio.run(data_source.load_metadata_rows())
        fetch.assert_awaited_once_with()
        return result

    return run


def records(path):
    return json.loads(path.read_text(encoding="utf-8"))


def field_rows(fields, tables=1):
    return [dict(ROW, table_name=f"t{i % tables}", column_name=f"c{i}") for i in range(fields)]


def seed_days(load, counts, tables=1):
    for day, count in enumerate(counts, start=1):
        load(field_rows(count, tables), f"202611{day:02}")


def test_first_complete_snapshot_preserves_rows_and_initializes_history(load, history, caplog):
    caplog.set_level(logging.INFO)
    rows, snapshot = load([dict(ROW, table_name=" orders "), dict(ROW, database_name="other")])
    assert rows == [MetadataRow(**ROW), MetadataRow(**dict(ROW, database_name="other"))]
    assert snapshot == "20261123"
    record, = records(history)
    assert (record["snapshot_pt"], record["raw_row_count"], record["field_count"],
            record["table_count"]) == (snapshot, 2, 2, 2)
    assert "基线初始化" in caplog.text


def test_empty_complete_snapshot_reports_date_and_does_not_write(load, history):
    with pytest.raises(ValueError, match="20261123.*空"):
        load([])
    assert not history.exists()


@pytest.mark.parametrize("key", ["database_name", "table_name", "column_name"])
def test_blank_identifier_fails_without_history(key, load, history):
    with pytest.raises(ValueError, match="第 2 行.*为空"):
        load([ROW, dict(ROW, **{key: "  "})])
    assert not history.exists()


@pytest.mark.parametrize("bad", [None, {}, dict(ROW, column_comment=None), dict(ROW, column_type=2)])
def test_invalid_structure_blocked(bad, load, history):
    with pytest.raises(ValueError, match="第 2 行.*必需字符串"):
        load([ROW, bad])
    assert not history.exists()


def test_normalized_duplicates_and_empty_attributes_counted_per_unique_field(load, history, caplog):
    blank = dict(ROW, table_comment="", column_comment="", column_type="")
    duplicate = dict(blank, column_name=" id ", column_type=" ")
    result, _ = load([blank, dict(blank, column_name="other"), duplicate])
    assert [r.column_name for r in result] == ["id", "other"]
    record, = records(history)
    assert [record[key] for key in ("raw_row_count", "field_count", "duplicate_count",
                                    "empty_table_comment_count", "empty_column_comment_count",
                                    "empty_column_type_count")] == [3, 2, 1, 2, 2, 2]
    assert "duplicate_count" in caplog.text and "empty_table_comment_count" in caplog.text


@pytest.mark.parametrize("override, conflict", [
    ({"column_type": "DECIMAL(18,2)"}, "column_type"),
    ({"column_comment": "private-value"}, "column_comment"),
    ({"table_comment": "private-value", "column_name": "other"}, "table_comment"),
])
def test_conflicting_definitions_fail_without_disclosing_values(override, conflict, load, history):
    with pytest.raises(ValueError, match=conflict) as failure:
        load([ROW, dict(ROW, **override)])
    expected_id = ("zz", "orders") if conflict == "table_comment" else ("zz", "orders", "id")
    assert repr(expected_id) in str(failure.value)
    assert "private-value" not in str(failure.value)
    assert not history.exists()


def test_short_history_allows_drop_without_claiming_formal_check(load, history, caplog):
    caplog.set_level(logging.INFO)
    seed_days(load, [100] * 6)
    load(field_rows(1))
    assert len(records(history)) == 7
    assert "6/7" in caplog.text


def test_field_drop_with_outlier_uses_median_and_failed_snapshot_never_commits(load, history):
    seed_days(load, [7950, 7980, 8010, 8030, 8000, 6200, 8020])
    before = history.read_bytes()
    with pytest.raises(ValueError, match=r"field_count.*500.*8000"):
        load(field_rows(500))
    assert history.read_bytes() == before
    load(field_rows(5600))  # 30% exactly is accepted
    assert records(history)[-1]["field_count"] == 5600


def test_table_drop_blocks_even_when_field_count_is_stable(load, history):
    seed_days(load, [100] * 7, tables=10)
    with pytest.raises(ValueError, match=r"table_count.*6.*10"):
        load(field_rows(100, 6))
    assert len(records(history)) == 7
    load(field_rows(100, 7))


def test_configured_threshold_reports_both_failing_metrics(load, history, monkeypatch):
    monkeypatch.setenv("METADATA_QUALITY_MAX_DROP_FRACTION", "0.10")
    seed_days(load, [100] * 7, tables=10)
    with pytest.raises(ValueError, match=r"field_count.*80.*table_count.*8"):
        load(field_rows(80, 8))
    load(field_rows(90, 9))


def test_window_uses_last_seven_prior_dates_even_for_out_of_order_ingestion(load, history):
    for day, count in [(1, 1000), (2, 1000), (3, 1000), (4, 1000), (5, 10),
                       (6, 10), (7, 10), (13, 1000), (14, 1000), (15, 1000), (16, 1000)]:
        load(field_rows(count), f"202611{day:02}")
    # Insert day 8 with a count permitted by its early high baseline. Days 2..8
    # still have four high counts, so day 9 must reject a large drop.
    load(field_rows(700), "20261108")
    with pytest.raises(ValueError, match=r"field_count.*10.*700"):
        load(field_rows(10), "20261109")
    load(field_rows(490), "20261109")
    # Days 3..9 now have four lower counts; future high counts must be ignored.
    load(field_rows(343), "20261110")
    assert len(records(history)) == 14


def test_same_date_replay_works_outside_window_but_changed_duplicate_count_fails(load, history):
    seed_days(load, [1] * 9)
    before = history.read_bytes()
    rows = field_rows(1)
    _, returned = load(rows, "2026-11-01")
    assert returned == "2026-11-01" and history.read_bytes() == before
    with pytest.raises(ValueError, match="不一致"):
        load(rows + rows, "2026-11-01")
    assert history.read_bytes() == before


def test_opaque_snapshots_track_replays_without_contributing_dates(load, history, caplog):
    for day in range(7):
        load(field_rows(100), f"p{day}")
    load(field_rows(1), "20261123")
    rows, snapshot = load(field_rows(100), "p0")
    assert len(rows) == 100 and snapshot == "p0" and len(records(history)) == 8
    assert "日期" in caplog.text
    with pytest.raises(ValueError, match="不一致"):
        load(field_rows(1), "p0")


def test_opaque_snapshot_checked_against_recent_dated_baseline(load, history):
    seed_days(load, [100] * 7)
    with pytest.raises(ValueError, match=r"field_count.*50.*100"):
        load(field_rows(50), "latest-published")
    assert len(records(history)) == 7


def test_replayed_opaque_snapshot_is_rechecked_as_dated_history_grows(load, history):
    load(field_rows(10), "published")
    seed_days(load, [100] * 7)
    before = history.read_bytes()
    with pytest.raises(ValueError, match=r"field_count.*10.*100"):
        load(field_rows(10), "published")
    assert history.read_bytes() == before


def test_concurrent_process_cannot_overwrite_an_active_writer(load, history, monkeypatch):
    replace = os.replace
    probe = """
import asyncio
from unittest.mock import AsyncMock
from table_column_same_graph import data_source
data_source.fetch_metadata_pages = AsyncMock(return_value=([ROW], '20261122'))
try:
    asyncio.run(data_source.load_metadata_rows())
except OSError as exc:
    assert '锁' in str(exc), str(exc)
else:
    raise AssertionError('active history lock failed to exclude another process')
""".replace("[ROW]", repr([ROW]))

    def check_lock_and_replace(source, target):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        subprocess.run([sys.executable, "-c", probe], env=env, check=True, timeout=20)
        replace(source, target)

    monkeypatch.setattr(os, "replace", check_lock_and_replace)
    load([ROW])
    assert [r["snapshot_pt"] for r in records(history)] == ["20261123"]


@pytest.mark.parametrize("damage", ["json", "shape", "duplicate_day", "bool_count",
                                    "incoherent_count", "bad_timestamp", "missing_count"])
def test_corrupt_history_fails_closed_and_is_preserved(damage, load, history):
    load([ROW])
    data = records(history)
    if damage == "duplicate_day":
        data.append(dict(data[0], snapshot_pt="2026-11-23"))
    elif damage == "bool_count":
        data[0]["field_count"] = True
    elif damage == "incoherent_count":
        data[0]["field_count"] = 2
    elif damage == "bad_timestamp":
        data[0]["validated_at"] = "n/a"
    elif damage == "missing_count":
        del data[0]["duplicate_count"]
    elif damage == "shape":
        data = {}
    history.write_text("{" if damage == "json" else json.dumps(data), encoding="utf-8")
    before = history.read_bytes()
    with pytest.raises(ValueError, match="质量历史"):
        load([ROW], "20261124")
    assert history.read_bytes() == before


def test_atomic_replace_error_keeps_old_history_and_cleans_temp_file(load, history, monkeypatch):
    load([ROW])
    before = history.read_bytes()
    initial_files = set(history.parent.iterdir())

    def fail_replace(source, target):
        assert Path(source).parent == history.parent and Path(target) == history
        raise OSError("write failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="write failure"):
        load([ROW], "20261124")
    assert history.read_bytes() == before
    assert set(history.parent.iterdir()) == initial_files
