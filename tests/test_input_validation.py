import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from table_column_same_graph import parser

@pytest.mark.parametrize('query,intent', [
    ('表 dwd_trade_order_pay_detail_di','single_table'), ('表：订单支付明细','single_table'),
    ('字段 pay_amt_30d','single_column'), ('字段 支付金额','single_column'),
    ("字段 pay_amt_30d DECIMAL(18,2) COMMENT '支付金额'",'single_column'),
    ('CREATE TABLE orders (id STRING COMMENT \'编号\')','single_table'),
    ('表 `zz`.`orders`','single_table'),
    ('全表比对','all_tables'), ('全字段比对','all_columns'), ('全指标比对','all_columns')])
def test_valid(query, intent):
    result = parser.validate_input(query)
    assert result['validation_status'] == 'VALID'
    assert result['intent'] == intent
    if intent == 'single_table':
        assert result['source_table'].table_name or result['source_table'].table_comment
    elif intent == 'single_column':
        assert result['source_column'].column_name or result['source_column'].column_comment

@pytest.mark.parametrize('query,code', [
    ('','UNSUPPORTED'), ('表','MISSING_TARGET'), ('字段','MISSING_TARGET'),
    ('表：','MISSING_TARGET'), ('字段：','MISSING_TARGET'),
    ('CREATE TABLE','INVALID_DDL'), ('CREATE TABLE x (','INVALID_DDL'),
    ('帮我查一下 dwd_trade_order_pay_detail_di','FORMAT_ERROR'),
    ('帮我找设备告警相关的表','UNSUPPORTED'), ('查询一下这个表','UNSUPPORTED'),
    ('字段是什么意思','UNSUPPORTED'), ('今天天气如何','UNSUPPORTED'),
    ('全表比对 abc','UNSUPPORTED'), ('表 CREATE TABLE x (id)','INVALID_DDL'),
    ('表 ：','MISSING_TARGET'), ("CREATE TABLE x (id STRING) COMMENT '",'INVALID_DDL'),
    ('CREATE TABLE x (id STRING,)', 'INVALID_DDL')])
def test_invalid(query, code):
    result = parser.validate_input(query)
    assert result['validation_status'] == 'INVALID'
    assert result['error_code'] == code
    assert result['message'] and result['suggestion']

@pytest.mark.parametrize('query', ['', '表', '字段', '表：', '字段：', '表 ：', 'CREATE TABLE', 'CREATE TABLE x (', '查询一下这个表', '帮我找设备告警相关的表', '字段是什么意思', '今天天气如何', '全表比对 abc', '帮我查一下 dwd_trade_order_pay_detail_di', '表 CREATE TABLE x (id)', "CREATE TABLE x (id STRING) COMMENT '"])
def test_invalid_never_loads_metadata(monkeypatch, tmp_path, capsys, query):
    from table_column_same_graph import graph, cli
    loader = AsyncMock()
    monkeypatch.setattr(graph, 'load_metadata_rows', loader)
    downstream = []
    for name in ('load_memory_records', 'find_single_table_matches', 'find_all_table_matches', 'find_single_column_matches', 'find_all_column_matches', 'review_matches_with_llm', 'write_matches_to_csv', 'persist_memory'):
        mock = Mock(side_effect=AssertionError('invalid input reached ' + name))
        monkeypatch.setattr(graph, name, mock)
        downstream.append(mock)
    result = asyncio.run(graph.build_graph().ainvoke({'user_input':query, 'output_dir':str(tmp_path/'out'), 'memory_dir':str(tmp_path/'mem')}))
    assert result['validation_status'] == 'INVALID'
    loader.assert_not_awaited()
    assert 'output_file' not in result and 'memory_summary' not in result
    assert not (tmp_path/'out').exists() and not (tmp_path/'mem').exists()
    assert cli.main(['--query', query, '--output-dir', str(tmp_path/'out'), '--memory-dir', str(tmp_path/'mem')]) == 2
    output = capsys.readouterr().out
    assert '输入校验失败' in output and 'CSV 输出文件' not in output
    loader.assert_not_awaited()
    for mock in downstream:
        mock.assert_not_called()

def test_qualified_identifier_parsed():
    result = parser.validate_input('表 `zz`.`orders`')
    assert result['source_table'].table_name == 'orders'

def test_ddl_decimal_with_spaces_keeps_type():
    result = parser.validate_input('CREATE TABLE x (amount DECIMAL(18, 2))')
    assert result['source_table'].columns[0].column_type == 'DECIMAL(18, 2)'

def test_invalid_cli_exits_without_success_messages(monkeypatch, capsys):
    from table_column_same_graph import cli, graph
    loader = AsyncMock()
    monkeypatch.setattr(graph, 'load_metadata_rows', loader)
    assert cli.main(['--query', '字段']) == 2
    output = capsys.readouterr().out
    assert '输入校验失败' in output and '建议输入' in output
    assert 'CSV 输出文件' not in output and '记忆已沉淀' not in output
    loader.assert_not_awaited()
