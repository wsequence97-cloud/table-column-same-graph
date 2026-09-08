from table_column_same_graph.parser import detect_intent, parse_column_input, parse_table_input


def test_detect_full_table_intent():
    intent, has_ddl = detect_intent("全表比对")
    assert intent == "all_tables"
    assert has_ddl is False


def test_parse_table_ddl_extracts_table_comment_and_columns():
    ddl = """
    CREATE TABLE `zz`.`dwd_trade_order_pay_detail_di`(
      pay_id STRING COMMENT '支付号',
      pay_amt DECIMAL(18,2) COMMENT '支付金额'
    )
    COMMENT 'dwd-交易域-订单域-订单支付明细-增量表'
    """

    table = parse_table_input("表 " + ddl)

    assert table.table_name == "dwd_trade_order_pay_detail_di"
    assert table.table_comment == "dwd-交易域-订单域-订单支付明细-增量表"
    assert [column.column_name for column in table.columns] == ["pay_id", "pay_amt"]
    assert table.columns[1].column_comment == "支付金额"


def test_parse_column_definition_input():
    column = parse_column_input("字段 pay_amt_30d DECIMAL(18,2) COMMENT '支付金额'")

    assert column.column_name == "pay_amt_30d"
    assert column.column_type == "DECIMAL(18,2)"
    assert column.column_comment == "支付金额"
