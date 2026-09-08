from table_column_same_graph.llm_review import apply_column_decisions, apply_table_decisions, parse_model_json
from table_column_same_graph.models import ColumnMatch, TableMatch


def test_parse_model_json_from_fenced_block():
    content = """```json
    {"decisions":[{"index":0,"is_similar":true,"reason":"同一支付口径"}]}
    ```"""

    parsed = parse_model_json(content)

    assert parsed["decisions"][0]["is_similar"] is True


def test_apply_table_decisions_filters_false_candidates():
    matches = [
        TableMatch("a", "支付表", "b", "付款表", 0.9),
        TableMatch("a", "支付表", "c", "登录表", 0.7),
    ]

    kept = apply_table_decisions(
        matches,
        [
            {"index": 0, "is_similar": True, "reason": "支付和付款同义"},
            {"index": 1, "is_similar": False, "reason": "业务对象不同"},
        ],
    )

    assert [match.target_table_name for match in kept] == ["b"]
    assert kept[0].llm_reason == "支付和付款同义"


def test_apply_column_decisions_filters_missing_decision():
    matches = [
        ColumnMatch("pay_amt", "", "payment_amt", "t1", 1.0),
        ColumnMatch("pay_amt", "", "login_time", "t2", 0.8),
    ]

    kept = apply_column_decisions(matches, [{"index": 0, "is_similar": True, "reason": "支付金额同义"}])

    assert [match.target_column_name for match in kept] == ["payment_amt"]
    assert matches[1].llm_is_similar is False
