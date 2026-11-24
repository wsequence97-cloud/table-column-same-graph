import pytest

from table_column_same_graph import text_utils as text
from table_column_same_graph.models import ColumnInfo, ColumnMatch, TableMatch


@pytest.mark.parametrize("left,right", [
    ("defect_cnt", "defect_count"), ("alarm_count", "alarm_cnt"),
    ("alarm_amount", "alarm_amt"), ("ticket_number", "ticket_num"),
    ("failure_ratio", "failure_rate"),
    ("payment_amount", "pay_amt"), ("paid", "paying"),
])
def test_only_explicit_synonyms_unify(left, right):
    assert text.mixed_name_similarity(left, "", right, "") == 1


@pytest.mark.parametrize("left,right", [
    ("alarm_amount", "order_amt"), ("defect_count", "inspection_cnt"),
    ("order_number", "order_cnt"), ("utilization_ratio", "failure_rate"),
    ("alarm_count", "alarm_num"),
])
def test_common_term_or_business_object_alone_cannot_reach_field_threshold(left, right):
    assert text.mixed_name_similarity(left, "", right, "") == 0.5


def test_existing_chinese_payment_alias_survives():
    assert text.mixed_name_similarity("", "付款", "", "支付") == 1


@pytest.mark.parametrize("suffix", ["7d", "7day", "7days", "07DAYS"])
def test_daily_suffix_has_one_normalized_representation(suffix):
    raw = f"`db`.`alarm_count_{suffix}`"
    assert text.extract_window(raw) == "7d"
    assert text.name_features(raw, "", drop_window_suffix=True) == (frozenset({"alarm", "cnt"}), frozenset())


@pytest.mark.parametrize("name", [
    "alarm_cnt", "alarm_cnt_week", "alarm_7d_cnt", "alarm_cnt_1w",
    "alarm_cnt_7d_30d", "alarm_cnt_0d", "alarm_cnt_-7d",
])
def test_absent_and_ambiguous_windows_are_unknown(name):
    assert text.extract_window(name) is None
    assert text.window_relation(name, "alarm_cnt_7d") == "unknown"
    assert text.window_relation(name, name) == "unknown"


def test_window_difference_is_separate_from_business_similarity():
    left, right = "alarm_cnt_7days", "alarm_cnt_30d"
    assert text.window_relation(left, right) == "different"
    assert text.window_relation(left, "alarm_count_7day") == "same"
    assert text.compare_windows(None, None) == "unknown"
    assert text.mixed_name_similarity(left, "", right, "", drop_window_suffix=True) == 1


def test_feature_scores_preserve_direction_and_chinese_english_maximum():
    left = text.name_features("device_alarm", "缺陷")
    right = text.name_features("equipment_cnt", "设备缺陷")
    assert left == (frozenset({"device", "alarm"}), frozenset({"缺陷"}))
    forward = text.token_overlap_score(left[1], right[1])
    reverse = text.token_overlap_score(right[1], left[1])
    assert 0 < reverse < forward == 1
    assert text.feature_similarity(left, right) == forward
    assert text.bidirectional_feature_similarity(left, right) == (forward, reverse)
    empty = text.name_features("", "")
    assert text.bidirectional_feature_similarity(empty, left) == (0, 0)


def test_old_positional_model_constructors_keep_scores_and_decisions():
    column = ColumnInfo("id", "STRING", "编号", "orders", "订单")
    field_match = ColumnMatch("a", "ta", "b", "tb", 0.9, True, "相似")
    table = TableMatch("ta", "甲", "tb", "乙", 0.8, 0.7, False, "不相似")
    assert column.database_name == field_match.source_database_name == field_match.target_database_name == ""
    assert (field_match.similarity, field_match.llm_is_similar, field_match.llm_reason) == (0.9, True, "相似")
    assert (field_match.source_window, field_match.target_window, field_match.window_relation) == (None, None, "unknown")
    assert (table.field_similarity, table.llm_is_similar, table.llm_reason) == (0.7, False, "不相似")
    assert table.source_database_name == table.target_database_name == ""
    table.diagnostics["source_to_target"] = {}
    assert TableMatch("", "", "", "").diagnostics == {}
