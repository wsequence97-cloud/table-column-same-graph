import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
import ssl
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, INVALID_PARAMS, REQUEST_TIMEOUT

from table_column_same_graph import data_source, mcp_client
from table_column_same_graph.config import MCPConfig
from table_column_same_graph.mcp_client import fetch_from_client

TOOL = "query_field_metadata"
ROW = dict(database_name="zz", table_name="orders", table_comment="订单", column_name="id", column_type="STRING", column_comment="编号")
# Test-only negotiated application code. Production has no default expiry code.
EXPIRED = 7101


def page(snapshot="20261103", rows=None, cursor=None):
    return SimpleNamespace(is_error=False, structured_content={
        "snapshot_pt": snapshot, "rows": [ROW] if rows is None else rows, "next_cursor": cursor,
    })


def client_with(*responses):
    listing = SimpleNamespace(tools=[SimpleNamespace(name=TOOL)], next_cursor=None)
    return SimpleNamespace(list_tools=AsyncMock(return_value=listing), call_tool=AsyncMock(side_effect=responses))


def fetch(client, **overrides):
    config = replace(MCPConfig.from_env(), **overrides)
    return asyncio.run(fetch_from_client(client, TOOL, 1000, config=config))


def request(snapshot=None, cursor=None):
    return call(TOOL, {"snapshot_pt": snapshot, "page_size": 1000, "cursor": cursor})


@pytest.fixture(autouse=True)
def clean_mcp_config(monkeypatch):
    for name in ("MCP_PAGE_RETRIES", "MCP_RETRY_BACKOFF_SECONDS", "MCP_SNAPSHOT_RESTARTS",
                 "MCP_SNAPSHOT_EXPIRED_RPC_CODE", "MCP_PAGE_SIZE", "MCP_TOOL_NAME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sleep(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(mcp_client.asyncio, "sleep", mock)
    return mock


def test_single_page():
    client = client_with(page())
    assert fetch(client) == ([ROW], "20261103")
    assert client.call_tool.await_args_list == [request()]


def test_pages_fix_snapshot_and_combine():
    client = client_with(page(rows=[], cursor="next"), page())
    assert fetch(client) == ([ROW], "20261103")
    assert client.call_tool.await_args_list == [request(), request("20261103", "next")]


def test_empty_rows():
    assert fetch(client_with(page(rows=[]))) == ([], "20261103")


def test_missing_tool():
    client = client_with()
    client.list_tools.return_value = SimpleNamespace(tools=[], next_cursor=None)
    with pytest.raises(RuntimeError, match="未提供工具"):
        fetch(client)
    client.call_tool.assert_not_awaited()


@pytest.mark.parametrize("failure", [
    TimeoutError(), httpx2.ReadTimeout("interrupted"), ConnectionResetError(),
    MCPError(REQUEST_TIMEOUT, "request timed out"), MCPError(CONNECTION_CLOSED, "connection closed"),
    ExceptionGroup("transport", [httpx2.ReadTimeout("interrupted")]),
])
def test_retry_third_page_preserves_arguments_and_rows(failure, sleep):
    client = client_with(page(rows=[1], cursor="c2"), page(rows=[2], cursor="c3"), failure, page(rows=[3]))
    assert fetch(client) == ([1, 2, 3], "20261103")
    last = request("20261103", "c3")
    assert client.call_tool.await_args_list == [request(), request("20261103", "c2"), last, last]
    sleep.assert_awaited_once_with(1.0)


def test_retry_exhaustion_and_backoff(sleep):
    client = client_with(page(cursor="next"), TimeoutError(), TimeoutError(), TimeoutError())
    with pytest.raises(TimeoutError):
        fetch(client)
    assert client.call_tool.await_args_list == [request()] + [request("20261103", "next")] * 3
    assert sleep.await_args_list == [call(1.0), call(2.0)]


@pytest.mark.parametrize("failure", [
    PermissionError(), MCPError(INVALID_PARAMS, "invalid"), MCPError(7999, "snapshot expired"),
    ExceptionGroup("mixed", [httpx2.ReadTimeout("timeout"), PermissionError()]),
])
def test_deterministic_or_unknown_errors_do_not_retry(failure, sleep):
    client = client_with(page(cursor="next"), failure)
    with pytest.raises(type(failure)):
        fetch(client, snapshot_expired_rpc_code=EXPIRED)
    assert client.call_tool.await_count == 2
    sleep.assert_not_awaited()


@pytest.mark.parametrize("cause,retry", [(ConnectionRefusedError(), True), (ssl.SSLCertVerificationError(), False)])
def test_connect_error_requires_typed_transient_cause(cause, retry, sleep):
    failure = httpx2.ConnectError("connect failed")
    failure.__cause__ = cause
    client = client_with(failure, page())
    if retry:
        assert fetch(client) == ([ROW], "20261103")
        sleep.assert_awaited_once_with(1.0)
    else:
        with pytest.raises(httpx2.ConnectError):
            fetch(client)
        client.call_tool.assert_awaited_once()
        sleep.assert_not_awaited()


def test_tool_error_text_never_restarts_or_echoes_content():
    client = client_with(page(cursor="next"), SimpleNamespace(is_error=True, content=["snapshot expired: fake-secret"]))
    with pytest.raises(RuntimeError, match="返回错误") as error:
        fetch(client, snapshot_expired_rpc_code=EXPIRED)
    assert "fake-secret" not in str(error.value)
    assert client.call_tool.await_count == 2


def test_malformed_page():
    client = client_with(SimpleNamespace(is_error=False, structured_content={"rows": []}))
    with pytest.raises(ValueError, match="格式无效"):
        fetch(client)
    client.call_tool.assert_awaited_once()


def test_snapshot_mismatch():
    client = client_with(page(cursor="next"), page("20261104"))
    with pytest.raises(ValueError, match="不同的快照"):
        fetch(client)
    assert client.call_tool.await_count == 2


@pytest.mark.parametrize("bad_cursor", ["", "  ", 17, "next"])
def test_invalid_or_repeated_cursor(bad_cursor):
    client = client_with(page(cursor="next"), page(cursor=bad_cursor))
    with pytest.raises(ValueError, match="游标"):
        fetch(client)
    assert client.call_tool.await_count == 2


def test_snapshot_restart_discards_all_previous_state():
    client = client_with(page(rows=["old"], cursor="next"), MCPError(EXPIRED, "test"),
                         page("20261104", ["new1"], "next"), page("20261104", ["new2"]))
    assert fetch(client, snapshot_expired_rpc_code=EXPIRED) == (["new1", "new2"], "20261104")
    assert client.call_tool.await_args_list == [
        request(), request("20261103", "next"), request(), request("20261104", "next"),
    ]


def test_snapshot_restart_limit():
    failure = MCPError(EXPIRED, "test")
    client = client_with(page(cursor="n"), failure, page("20261104", cursor="n"), failure)
    with pytest.raises(MCPError) as error:
        fetch(client, snapshot_expired_rpc_code=EXPIRED)
    assert error.value.code == EXPIRED
    assert client.call_tool.await_count == 4


def test_snapshot_restart_disabled_without_contract():
    client = client_with(page(cursor="next"), MCPError(EXPIRED, "snapshot expired"))
    with pytest.raises(MCPError):
        fetch(client, snapshot_expired_rpc_code=None)
    assert client.call_tool.await_count == 2


def test_cancellation_propagates(sleep):
    client = client_with(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        fetch(client)
    client.call_tool.assert_awaited_once()
    sleep.assert_not_awaited()


def test_conversion_and_invalid_row(monkeypatch, tmp_path):
    monkeypatch.setenv("METADATA_QUALITY_HISTORY_PATH", str(tmp_path / "history.json"))
    monkeypatch.setattr(data_source, "fetch_metadata_pages", AsyncMock(return_value=([ROW], "snapshot")))
    rows, snapshot = asyncio.run(data_source.load_metadata_rows())
    assert rows[0].table_name == "orders" and snapshot == "snapshot"
    monkeypatch.setattr(data_source, "fetch_metadata_pages", AsyncMock(return_value=([{}], "snapshot")))
    with pytest.raises(ValueError, match="必需字符串"):
        asyncio.run(data_source.load_metadata_rows())

def test_transport_reopens_at_failed_page(monkeypatch, sleep):
    # A background transport failure cancels the SDK await; AnyIO reports the
    # actual failure only when its owning task group exits.
    first = client_with(page(cursor="n"), asyncio.CancelledError())
    second = client_with(page(rows=["new page"]))

    @asynccontextmanager
    async def lost_connection():
        try:
            yield first
        except asyncio.CancelledError:
            raise ExceptionGroup("transport", [httpx2.ReadError("reset")]) from None

    connections = iter([lost_connection(), nullcontext(second)])
    monkeypatch.setenv("MCP_SERVER_URL", "https://example.invalid")
    monkeypatch.setattr(mcp_client, "_connect", lambda config: next(connections))
    assert asyncio.run(mcp_client.fetch_metadata_pages()) == ([ROW, "new page"], "20261103")
    first.call_tool.assert_any_await(*request("20261103", "n").args)
    second.call_tool.assert_awaited_once_with(*request("20261103", "n").args)
    sleep.assert_awaited_once_with(1.0)


def test_public_boundary_reports_rpc_code_without_sensitive_details(monkeypatch, sleep):
    error = MCPError(INVALID_PARAMS, "remote text contains fake-secret", {"key": "fake-secret"})
    client = client_with(ExceptionGroup("fake-secret", [error]))
    monkeypatch.setenv("MCP_SERVER_URL", "https://example.invalid")
    monkeypatch.setattr(mcp_client, "_connect", lambda config: nullcontext(client))
    with pytest.raises(RuntimeError, match=str(INVALID_PARAMS)) as caught:
        asyncio.run(mcp_client.fetch_metadata_pages())
    assert "fake-secret" not in "".join(traceback.format_exception(caught.value))
    client.call_tool.assert_awaited_once()
    sleep.assert_not_awaited()
