from unittest.mock import patch

import pytest

from table_column_same_graph import similarity as sim
from table_column_same_graph.models import ColumnInfo, MetadataRow, TableInfo


def row(db, table, column, comment=""):
    return MetadataRow(db, table, "", column, "STRING", comment)


def test_full_table_bidirectional_name_and_fields_are_order_independent():
    # Canonical physical order deliberately puts the more specific name first.
    rows = [row("a", "device_alarm_count_extra", "alarm_count"),
            row("b", "device", "alarm")]
    assert sim.find_single_table_matches(TableInfo(table_name="device_alarm_count_extra"), rows) == []
    forward = sim.find_all_table_matches(rows)
    assert forward == sim.find_all_table_matches(rows[::-1])
    match, = forward
    assert (match.source_database_name, match.target_database_name) == ("a", "b")
    assert match.name_similarity == match.field_similarity == 1.0
    assert match.diagnostics["source_to_target"]["name_score"] == 0.25
    assert match.diagnostics["target_to_source"]["name_score"] == 1.0
    assert match.diagnostics["source_to_target"]["field_coverage"] == 0.0
    assert match.diagnostics["target_to_source"]["field_coverage"] == 1.0


def test_single_ddl_coverage_remains_directional():
    source = TableInfo(table_name="device", columns=[ColumnInfo(column_name="alarm_count")])
    assert not sim.find_single_table_matches(source, [row("b", "device_alarm", "alarm")])
    assert sim.table_field_similarity([], source.columns) == 0.0
    assert sim.table_field_similarity(source.columns, []) == 0.0


def test_many_to_one_mapping_and_windows_are_directional():
    rows = [row("a", "alarm", "alarm_cnt_" + window) for window in ("30d", "7d", "1d")]
    rows.append(row("b", "alarm", "alarm_cnt_1d"))
    match, = sim.find_all_table_matches(rows)
    assert sim.find_all_table_matches(rows[::-1]) == [match]
    assert match.field_similarity == 1.0
    forward = match.diagnostics["source_to_target"]
    reverse = match.diagnostics["target_to_source"]
    assert forward["many_to_one"] and not reverse["many_to_one"]
    assert forward["field_coverage"] == reverse["field_coverage"] == 1.0
    mappings = forward["best_matches"]
    assert {m["source_column"] for m in mappings} == {"alarm_cnt_1d", "alarm_cnt_7d", "alarm_cnt_30d"}
    assert {m["target_column"] for m in mappings} == {"alarm_cnt_1d"}
    assert {m["source_window"]: m["window_relation"] for m in mappings} == {
        "1d": "same", "7d": "different", "30d": "different"}


def test_tied_scores_use_database_and_table_order():
    rows = [row(db, "alarm", "alarm") for db in ("c", "a", "b")]
    expected = [("a", "b"), ("a", "c"), ("b", "c")]
    matches = sim.find_all_table_matches(rows)
    assert [(m.source_database_name, m.target_database_name) for m in matches] == expected
    assert matches == sim.find_all_table_matches(rows[::-1])


def test_database_qualified_self_match_skips_only_exact_identity():
    rows = [row(db, "alarm", "alarm") for db in ("b", "a")]
    match, = sim.find_single_table_matches(TableInfo(table_name="alarm", database_name="a"), rows)
    assert match.target_database_name == "b"
    assert [m.target_database_name for m in sim.find_single_table_matches(TableInfo(table_name="alarm"), rows)] == ["a", "b"]
    col, = sim.find_all_column_matches(rows)
    assert {col.source_database_name, col.target_database_name} == {"a", "b"}


@pytest.mark.parametrize("target,window,relation", [
    ("alarm_count_7days", "7d", "same"),
    ("alarm_cnt_30d", "30d", "different"),
    ("alarm_cnt", None, "unknown"),
])
def test_column_window_diagnostics_do_not_filter_candidates(target, window, relation):
    source = ColumnInfo(column_name="alarm_cnt_7d")
    match, = sim.find_single_column_matches(source, [row("db", "target", target)])
    assert (match.source_column_name, match.target_column_name) == ("alarm_cnt_7d", target)
    assert (match.source_window, match.target_window, match.window_relation) == ("7d", window, relation)
    assert match.similarity == 1.0


def test_all_columns_preserve_original_direction():
    a, b = row("a", "alarm", "alarm_count"), row("b", "device", "alarm")
    assert sim.find_all_column_matches([a, b]) == []
    match, = sim.find_all_column_matches([b, a])
    assert match.similarity == 1.0


def test_preprocessing_count_scales_with_assets_not_pairs():
    rows = [row(str(i), "device", "alarm_" + str(i)) for i in range(8)]
    with patch.object(sim, "name_features", wraps=sim.name_features) as tokenize:
        sim.find_all_table_matches(rows)
        assert tokenize.call_count <= len(rows) * 2
    with patch.object(sim, "name_features", wraps=sim.name_features) as tokenize:
        sim.find_all_column_matches(rows)
        assert tokenize.call_count <= len(rows)


@pytest.mark.parametrize("bidirectional", [False, True])
def test_cached_and_uncached_table_scores_and_mappings_are_identical(bidirectional):
    rows = [
        row("one", "device_alarm", "alarm_cnt_1d", "告警数量"),
        row("one", "device_alarm", "alarm_cnt_7d", "告警数量"),
        row("two", "device_alarm_stats", "alarm_count_30days", "设备告警数量"),
        row("two", "device_alarm_stats", "serial_num", "设备序号"),
    ]
    cached = sim._find_all_table_matches(rows, use_cache=True, bidirectional=bidirectional)
    uncached = sim._find_all_table_matches(rows, use_cache=False, bidirectional=bidirectional)
    assert cached
    assert cached == uncached


def test_name_filter_skips_field_comparisons():
    rows = [row("a", "billing", "alarm"), row("b", "device", "alarm")]
    with patch.object(sim, "_field_coverages", wraps=sim._field_coverages) as fields:
        assert sim.find_all_table_matches(rows) == []
        fields.assert_not_called()
