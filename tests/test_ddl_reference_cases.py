"""按对照工作簿 D01–D07 的场景和计数重建。字段定义为测试样例，原始 DDL 未提供。"""

from itertools import permutations

import pytest

from table_column_same_graph.config import SIMILARITY_CONFIG
from table_column_same_graph.models import ColumnInfo, MetadataRow
from table_column_same_graph.similarity import column_similarity, find_all_table_matches, table_field_similarity


def field(name, comment="", dtype="STRING"):
    return ColumnInfo(column_name=name, column_comment=comment, column_type=dtype)


CASES = [
    pytest.param(
        [field("device_no"), field("alarm_code"), field("gateway_key")],
        [field("device_no"), field("alarm_code"), field("station_seq")],
        2 / 3, 2 / 3, 2 / 3, False, id="D01-two-of-three",
    ),
    pytest.param(
        [field("alarm_cnt_1d"), field("alarm_cnt_7d"), field("alarm_cnt_30d")], [field("alarm_cnt_1d")],
        1.0, 1.0, 1 / 3, True, id="D02-three-windows-one-target",
    ),
    pytest.param(
        [field("fault_code"), field("line_no")],
        [field("fault_code"), field("line_no"), field("start_ts"), field("area_name")],
        1.0, 0.5, 1.0, False, id="D03-two-fields-contained-in-four",
    ),
    pytest.param(
        [field("event_code", dtype="INT")], [field("event_code", dtype="STRING")],
        1.0, 1.0, 1.0, False, id="D04-different-types",
    ),
    pytest.param(
        [field("line_num", "线路编号")], [field("feeder_key", "线路编号")],
        1.0, 1.0, 1.0, False, id="D05-identical-comments",
    ),
    pytest.param(
        [field("alarm_cnt")], [field("defect_cnt")],
        0.0, 0.0, 0.0, False, id="D06-different-business-events",
    ),
    pytest.param(
        [field("state_flag", "设备在线状态")], [field("state_flag", "工单审核状态")],
        1.0, 1.0, 1.0, False, id="D07-identical-name-different-state-definitions",
    ),
]


def one_to_one_reference(source, target):
    """对最多四个字段穷举唯一分配，作为独立的一对一结构覆盖率测试参照。"""
    def hits(assignment):
        return sum(column_similarity(a, b) >= SIMILARITY_CONFIG.table_column_threshold
                   for a, b in assignment)

    if len(source) <= len(target):
        assignments = (zip(source, order) for order in permutations(target, len(source)))
    else:
        assignments = (zip(order, target) for order in permutations(source, len(target)))
    return max(hits(assignment) for assignment in assignments) / len(source)


@pytest.mark.parametrize("source,target,forward,reverse,one_to_one,risk", CASES)
def test_reconstructed_ddl_reference(source, target, forward, reverse, one_to_one, risk):
    assert table_field_similarity(source, target) == pytest.approx(forward)
    assert table_field_similarity(target, source) == pytest.approx(reverse)
    assert one_to_one_reference(source, target) == pytest.approx(one_to_one)

    # 同名跨库表通过名称召回，以隔离并检验字段阶段和输出的方向性诊断。
    rows = [MetadataRow(db, "asset", "", c.column_name, c.column_type, c.column_comment)
            for db, columns in [("a", source), ("b", target)] for c in columns]
    matches = find_all_table_matches(rows)
    if max(forward, reverse) < SIMILARITY_CONFIG.all_table_field_threshold:
        assert matches == []
        return

    match, = matches
    assert match.field_similarity == pytest.approx(max(forward, reverse))
    forward_report = match.diagnostics["source_to_target"]
    reverse_report = match.diagnostics["target_to_source"]
    assert forward_report["field_coverage"] == pytest.approx(forward)
    assert reverse_report["field_coverage"] == pytest.approx(reverse)
    assert forward_report["many_to_one"] is risk
    assert reverse_report["many_to_one"] is False
