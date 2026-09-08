from table_column_same_graph.models import ColumnInfo, MetadataRow, TableInfo
from table_column_same_graph.similarity import (
    column_similarity,
    find_all_table_matches,
    find_single_table_matches,
    table_field_similarity,
)


def _rows():
    return [
        MetadataRow("zz", "dwd_trade_order_pay_detail_di", "dwd-交易域-订单域-订单支付明细-增量表", "pay_id", "string", "支付号"),
        MetadataRow("zz", "dwd_trade_order_pay_detail_di", "dwd-交易域-订单域-订单支付明细-增量表", "order_id", "string", "订单号"),
        MetadataRow("zz", "dwd_trade_order_pay_detail_di", "dwd-交易域-订单域-订单支付明细-增量表", "pay_amt", "decimal", "支付金额"),
        MetadataRow("zz", "dwd_trade_order_payment_detail_di", "dwd-交易域-订单域-订单付款明细-增量表", "payment_id", "string", "付款号"),
        MetadataRow("zz", "dwd_trade_order_payment_detail_di", "dwd-交易域-订单域-订单付款明细-增量表", "order_id", "string", "订单号"),
        MetadataRow("zz", "dwd_trade_order_payment_detail_di", "dwd-交易域-订单域-订单付款明细-增量表", "payment_amt", "decimal", "付款金额"),
    ]


def test_column_similarity_strips_window_suffix():
    left = ColumnInfo(column_name="pay_amt_30d", column_comment="支付金额")
    right = ColumnInfo(column_name="pay_amt_90d", column_comment="支付金额")

    assert column_similarity(left, right) >= 0.75


def test_single_table_name_match():
    source = TableInfo(table_name="dwd_trade_order_pay_detail_di", table_comment="dwd-交易域-订单域-订单支付明细-增量表")

    matches = find_single_table_matches(source, _rows())

    assert matches
    assert matches[0].target_table_name == "dwd_trade_order_payment_detail_di"


def test_table_field_similarity_reaches_threshold():
    source_columns = [
        ColumnInfo(column_name="pay_id", column_comment="支付号"),
        ColumnInfo(column_name="order_id", column_comment="订单号"),
        ColumnInfo(column_name="pay_amt", column_comment="支付金额"),
    ]
    target_columns = [
        ColumnInfo(column_name="payment_id", column_comment="付款号"),
        ColumnInfo(column_name="order_id", column_comment="订单号"),
        ColumnInfo(column_name="payment_amt", column_comment="付款金额"),
    ]

    assert table_field_similarity(source_columns, target_columns) >= 0.60


def test_all_table_matches_are_deduped_directionally():
    matches = find_all_table_matches(_rows())
    pairs = {(match.source_table_name, match.target_table_name) for match in matches}
    reverse_pairs = {(right, left) for left, right in pairs}

    assert pairs
    assert not (pairs & reverse_pairs)
