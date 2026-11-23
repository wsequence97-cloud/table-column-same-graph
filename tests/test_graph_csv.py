import asyncio
import csv
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

pytest.importorskip("langgraph")

from table_column_same_graph import data_source, graph
from table_column_same_graph.llm_review import OpenAICompatibleClient
from table_column_same_graph.models import MetadataRow


ROW = dict(database_name="zz", table_name="orders", table_comment="订单",
           column_name="pay_amt", column_type="STRING", column_comment="支付金额")
ROWS = [ROW, dict(ROW, table_name="orders_backup")]


@pytest.fixture(autouse=True)
def history_path(tmp_path, monkeypatch):
    path = tmp_path / "quality" / "history.json"
    monkeypatch.setenv("METADATA_QUALITY_HISTORY_PATH", str(path))
    monkeypatch.delenv("METADATA_QUALITY_MAX_DROP_FRACTION", raising=False)
    return path


def run_graph(query, tmp_path, **settings):
    return asyncio.run(graph.build_graph().ainvoke({
        "user_input": query,
        "output_dir": str(tmp_path / "outputs"),
        "memory_dir": str(tmp_path / "memory"),
        "llm_review_mode": "required",
        "llm_api_base": "https://example.invalid",
        "llm_model": "test-model",
        "llm_batch_size": 20,
        **settings,
    }))


@pytest.mark.parametrize(("query", "intent", "match_key"), [
    ("表：订单", "single_table", "table_matches"),
    ("全表比对", "all_tables", "table_matches"),
    ("字段 pay_amt", "single_column", "column_matches"),
    ("全字段比对", "all_columns", "column_matches"),
])
def test_graph_uses_mcp_loader_and_writes_reviewed_matches(tmp_path, monkeypatch, history_path,
                                                         query, intent, match_key):
    fetch = AsyncMock(return_value=([*ROWS, ROW], "2026-11-03"))
    monkeypatch.setattr(data_source, "fetch_metadata_pages", fetch)
    review = Mock(return_value={"decisions": [{"index": 0, "is_similar": True, "reason": "同一口径"}]})
    monkeypatch.setattr(OpenAICompatibleClient, "complete_json", review)

    result = run_graph(query, tmp_path)

    fetch.assert_awaited_once()
    review.assert_called_once()
    assert result["intent"] == intent
    assert result["metadata_snapshot_pt"] == "2026-11-03"
    assert result["metadata_rows"] == [MetadataRow(**row) for row in ROWS]
    assert result["llm_review_summary"]["kept"] == len(result[match_key]) == 1
    with Path(result["output_file"]).open(encoding="utf-8-sig", newline="") as file:
        emitted = list(csv.reader(file))
    assert len(emitted) == 2
    match = result[match_key][0]
    assert match.target_table_name in emitted[1]
    memory = json.loads((tmp_path / "memory" / "similarity_memory.jsonl").read_text(encoding="utf-8"))
    assert memory == result["memory_summary"]
    assert memory["top_matches"][0]["llm_reason"] == "同一口径"
    assert history_path.is_file()


@pytest.fixture
def downstream_nodes(monkeypatch):
    spies = []
    for name in ("load_memory", "match_single_table", "match_all_tables", "match_single_column",
                 "match_all_columns", "llm_review", "write_csv", "persist_memory"):
        attr = f"{name}_node"
        spy = create_autospec(getattr(graph, attr), side_effect=AssertionError(f"Unexpected node {name}"))
        monkeypatch.setattr(graph, attr, spy)
        spies.append(spy)
    return spies


def assert_no_downstream_effects(tmp_path, downstream_nodes):
    for spy in downstream_nodes:
        spy.assert_not_called()
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "memory").exists()


@pytest.mark.parametrize(("response", "expected", "message"), [
    (RuntimeError("MCP unavailable"), RuntimeError, "MCP unavailable"),
    (([], "20261108"), ValueError, "20261108"),
    (([ROW, dict(ROW, column_type="INT")], "20261108"), ValueError, None),
], ids=["read_failure", "empty", "conflict"])
def test_bad_metadata_stops_downstream(tmp_path, monkeypatch, history_path, downstream_nodes,
                                      response, expected, message):
    fetch = AsyncMock(side_effect=[response])
    monkeypatch.setattr(data_source, "fetch_metadata_pages", fetch)

    with pytest.raises(expected, match=message):
        run_graph("全表比对", tmp_path)

    fetch.assert_awaited_once()
    assert_no_downstream_effects(tmp_path, downstream_nodes)
    assert not history_path.exists()


def test_count_drop_stops_graph_and_preserves_baseline(tmp_path, monkeypatch, history_path, downstream_nodes):
    fetch = AsyncMock()
    monkeypatch.setattr(data_source, "fetch_metadata_pages", fetch)
    for day in range(1, 8):
        fetch.return_value = (ROWS, f"202611{day:02}")
        asyncio.run(data_source.load_metadata_rows())
    baseline = history_path.read_bytes()
    fetch.return_value = ([ROW], "20261108")

    with pytest.raises(ValueError):
        run_graph("全表比对", tmp_path)

    assert fetch.await_count == 8
    assert_no_downstream_effects(tmp_path, downstream_nodes)
    assert history_path.read_bytes() == baseline


def test_invalid_input_skips_mcp_and_quality_history(tmp_path, monkeypatch, history_path, downstream_nodes):
    fetch = AsyncMock()
    monkeypatch.setattr(data_source, "fetch_metadata_pages", fetch)

    assert run_graph("字段", tmp_path)["validation_status"] == "INVALID"

    fetch.assert_not_awaited()
    assert_no_downstream_effects(tmp_path, downstream_nodes)
    assert not history_path.exists()


def test_validated_history_survives_required_llm_failure(tmp_path, monkeypatch, history_path):
    fetch = AsyncMock(return_value=(ROWS, "20261103"))
    monkeypatch.setattr(data_source, "fetch_metadata_pages", fetch)
    review = Mock(side_effect=RuntimeError("review unavailable"))
    monkeypatch.setattr(OpenAICompatibleClient, "complete_json", review)

    with pytest.raises(RuntimeError, match="review unavailable"):
        run_graph("全表比对", tmp_path)

    review.assert_called_once()
    assert "20261103" in history_path.read_text(encoding="utf-8")
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "memory").exists()


def test_graph_calls_mcp_client(tmp_path, monkeypatch):
    import httpx2
    import mcp
    import mcp.client.streamable_http as transport

    tool = "query_field_metadata"
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name=tool)], next_cursor=None)),
        call_tool=AsyncMock(return_value=SimpleNamespace(is_error=False, structured_content={
            "snapshot_pt": "p1", "rows": [ROW], "next_cursor": None,
        })),
    )
    sdk_client = Mock(return_value=nullcontext(client))
    monkeypatch.setenv("MCP_SERVER_URL", "https://example.invalid/mcp")
    monkeypatch.setenv("MCP_TIMEOUT_SECONDS", "13")
    monkeypatch.setenv("MCP_TOOL_NAME", tool)
    monkeypatch.setenv("MCP_PAGE_SIZE", "1000")
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(transport, "streamable_http_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(mcp, "Client", sdk_client)

    result = run_graph("字段 pay_amt", tmp_path, llm_review_mode="disabled")

    assert result["metadata_snapshot_pt"] == "p1"
    assert sdk_client.call_args.kwargs["read_timeout_seconds"] == 13
    client.call_tool.assert_awaited_once_with(tool, {"snapshot_pt": None, "page_size": 1000, "cursor": None})
