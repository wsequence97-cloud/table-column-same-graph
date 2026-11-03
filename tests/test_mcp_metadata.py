import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from table_column_same_graph.mcp_client import fetch_from_client
from table_column_same_graph import data_source

ROW = dict(database_name="zz", table_name="orders", table_comment="订单", column_name="id", column_type="STRING", column_comment="编号")

def run(coro):
    return asyncio.run(coro)

def client_with(*pages):
    client = SimpleNamespace()
    client.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="query_field_metadata")], next_cursor=None))
    client.call_tool = AsyncMock(side_effect=[SimpleNamespace(is_error=False, structured_content=p, content=[]) for p in pages])
    return client

def test_pages_fix_snapshot_and_combine():
    client = client_with(dict(snapshot_pt="2026-11-03", rows=[ROW], next_cursor="next"), dict(snapshot_pt="2026-11-03", rows=[ROW], next_cursor=None))
    rows, snapshot = run(fetch_from_client(client, "query_field_metadata", 1000))
    assert len(rows) == 2 and snapshot == "2026-11-03"
    assert client.call_tool.await_args_list[1].args[1]["snapshot_pt"] == snapshot

def test_empty_rows():
    client = client_with(dict(snapshot_pt="2026-11-03", rows=[], next_cursor=None))
    assert run(fetch_from_client(client, "query_field_metadata", 1000)) == ([], "2026-11-03")

def test_missing_tool():
    client = client_with()
    client.list_tools.return_value = SimpleNamespace(tools=[], next_cursor=None)
    with pytest.raises(RuntimeError, match="未提供工具"):
        run(fetch_from_client(client, "query_field_metadata", 1000))

def test_tool_error():
    client = client_with()
    client.call_tool = AsyncMock(return_value=SimpleNamespace(is_error=True, content=["denied"], structured_content=None))
    with pytest.raises(RuntimeError, match="返回错误"):
        run(fetch_from_client(client, "query_field_metadata", 1000))

def test_snapshot_mismatch():
    client = client_with(dict(snapshot_pt="one", rows=[], next_cursor="next"), dict(snapshot_pt="two", rows=[], next_cursor=None))
    with pytest.raises(ValueError, match="不同的快照"):
        run(fetch_from_client(client, "query_field_metadata", 1000))

def test_conversion_and_invalid_row(monkeypatch):
    monkeypatch.setattr(data_source, "fetch_metadata_pages", AsyncMock(return_value=([ROW], "snapshot")))
    rows, snapshot = run(data_source.load_metadata_rows())
    assert rows[0].table_name == "orders" and snapshot == "snapshot"
    monkeypatch.setattr(data_source, "fetch_metadata_pages", AsyncMock(return_value=([{}], "snapshot")))
    with pytest.raises(ValueError, match="必需字符串"):
        run(data_source.load_metadata_rows())
