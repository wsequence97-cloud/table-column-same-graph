import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
pytest.importorskip("langgraph")

from table_column_same_graph.graph import build_graph
from table_column_same_graph.models import MetadataRow
from table_column_same_graph import data_source

@pytest.mark.parametrize("query", ["表 orders", "全表比对", "字段 pay_amt", "全字段比对"])
def test_graph_uses_mcp_loader_and_writes_outputs(tmp_path, monkeypatch, query):
    row = dict(database_name="zz", table_name="orders_v2", table_comment="订单", column_name="pay_amt", column_type="STRING", column_comment="支付金额")
    mock = AsyncMock(return_value=([row], "2026-11-03"))
    monkeypatch.setattr(data_source, "fetch_metadata_pages", mock)
    result = asyncio.run(build_graph().ainvoke(dict(user_input=query, output_dir=str(tmp_path / "outputs"), memory_dir=str(tmp_path / "memory"), llm_review_mode="disabled")))
    assert result["metadata_rows"] == [MetadataRow(**row)]
    assert result["metadata_snapshot_pt"] == "2026-11-03"
    assert Path(result["output_file"]).exists()
    assert (tmp_path / "memory" / "similarity_memory.jsonl").exists()
    mock.assert_awaited_once()

def test_graph_propagates_mcp_failure(monkeypatch):
    monkeypatch.setattr(data_source, "fetch_metadata_pages", AsyncMock(side_effect=RuntimeError("MCP unavailable")))
    with pytest.raises(RuntimeError, match="MCP unavailable"):
        asyncio.run(build_graph().ainvoke(dict(user_input="全表比对")))

def test_graph_calls_mcp_client(tmp_path, monkeypatch):
    import mcp
    import httpx2
    import mcp.client.streamable_http as transport
    from types import SimpleNamespace
    row = dict(database_name="zz", table_name="orders", table_comment="订单", column_name="pay_amt", column_type="STRING", column_comment="支付金额")
    client = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="query_field_metadata")], next_cursor=None)), call_tool=AsyncMock(return_value=SimpleNamespace(is_error=False, structured_content=dict(snapshot_pt="p1", rows=[row], next_cursor=None))))
    class Context:
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self.value
        async def __aexit__(self, *args): pass
    monkeypatch.setenv("MCP_SERVER_URL", "https://example.invalid/mcp")
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: Context(object()))
    monkeypatch.setattr(transport, "streamable_http_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(mcp, "Client", lambda *args: Context(client))
    result = asyncio.run(build_graph().ainvoke(dict(user_input="字段 pay_amt", output_dir=str(tmp_path / "o"), memory_dir=str(tmp_path / "m"), llm_review_mode="disabled")))
    assert result["metadata_snapshot_pt"] == "p1"
    client.call_tool.assert_awaited_once_with("query_field_metadata", {"snapshot_pt": None, "page_size": 1000, "cursor": None})
