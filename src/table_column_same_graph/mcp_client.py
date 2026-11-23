"""使用官方 MCP SDK 读取同一快照的全部分页。"""
import asyncio
from contextlib import asynccontextmanager, nullcontext
import logging

from table_column_same_graph.config import MCPConfig


logger = logging.getLogger(__name__)


@asynccontextmanager
async def _connect(config):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {config.auth_token}"} if config.auth_token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=config.timeout_seconds) as http:
        transport = streamable_http_client(config.server_url, http_client=http)
        async with Client(transport, read_timeout_seconds=config.timeout_seconds) as client:
            yield client


class _PageFormatError(ValueError):
    """本地生成的协议校验诊断。"""


class _ToolError(RuntimeError):
    """本地生成的工具发现或执行诊断。"""


def _safe_reason(exc):
    from mcp.shared.exceptions import MCPError

    if isinstance(exc, ExceptionGroup):
        return "、".join(dict.fromkeys(_safe_reason(child) for child in exc.exceptions))
    if isinstance(exc, MCPError):
        return f"MCPError (RPC {exc.code})"
    if isinstance(exc, (_PageFormatError, _ToolError)):
        return str(exc)
    return type(exc).__name__


async def fetch_metadata_pages():
    config = MCPConfig.from_env()
    if not config.server_url or not config.tool_name or config.timeout_seconds <= 0 or config.page_size <= 0:
        raise ValueError("请配置有效的 MCP_SERVER_URL、MCP_TOOL_NAME、超时和分页大小")
    try:
        return await _fetch(lambda: _connect(config), config.tool_name, config.page_size, config)
    except Exception as exc:
        # SDK exceptions and group messages can include headers, tool content or URLs.
        reason = _safe_reason(exc)
        raise RuntimeError(f"MCP 元数据读取失败：{reason}") from None


async def fetch_from_client(client, tool, page_size, *, config=None):
    """复用已连接的 Client，与生产入口共享分页、恢复及校验逻辑。"""
    return await _fetch(lambda: nullcontext(client), tool, page_size, config or MCPConfig.from_env())


async def _require_tool(client, tool):
    cursor, seen = None, set()
    while True:
        listing = await client.list_tools(cursor=cursor)
        if any(item.name == tool for item in listing.tools):
            return
        cursor = listing.next_cursor
        if cursor is None:
            raise _ToolError("MCP Server 未提供工具，请核对 MCP_TOOL_NAME")
        if not isinstance(cursor, str) or not cursor.strip() or cursor in seen:
            raise _ToolError("MCP 工具列表游标无效或重复")
        seen.add(cursor)


def _transient(exc):
    import httpx2
    from mcp.shared.exceptions import MCPError
    from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

    # AnyIO may surface transport task failures only when the SDK context exits.
    if isinstance(exc, ExceptionGroup):
        return all(_transient(child) for child in exc.exceptions)
    if isinstance(exc, MCPError):
        return exc.code in (REQUEST_TIMEOUT, CONNECTION_CLOSED)
    if isinstance(exc, (TimeoutError, ConnectionError, httpx2.TimeoutException, httpx2.ReadError, httpx2.WriteError)):
        return True
    # A generic ConnectError can also mean TLS verification failed. Accept only
    # a typed temporary network cause through httpx2/httpcore2 wrappers.
    if isinstance(exc, httpx2.ConnectError):
        cause = exc.__cause__
        while cause is not None:
            if isinstance(cause, (TimeoutError, ConnectionError)):
                return True
            cause = cause.__cause__
    return False


def _validate_page(result, snapshot, seen):
    if result.is_error:
        # Tool content is unstructured and can contain credentials or request data.
        raise _ToolError("MCP 元数据工具返回错误（is_error）")
    data = result.structured_content
    if (not isinstance(data, dict) or not isinstance(data.get("snapshot_pt"), str)
            or not data["snapshot_pt"].strip() or not isinstance(data.get("rows"), list)
            or "next_cursor" not in data):
        raise _PageFormatError("MCP 工具返回的快照、rows 或 next_cursor 格式无效")
    if snapshot is not None and snapshot != data["snapshot_pt"]:
        raise _PageFormatError("MCP 分页返回了不同的快照")
    cursor = data["next_cursor"]
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip() or cursor in seen):
        raise _PageFormatError("MCP 分页游标无效或重复")
    return data["snapshot_pt"], data["rows"], cursor


async def _backoff(config, retries):
    logger.warning("MCP 元数据分页临时故障，重试 %d/%d", retries + 1, config.page_retries)
    await asyncio.sleep(config.retry_backoff_seconds * 2 ** retries)


async def _fetch(connect, tool, page_size, config):
    from mcp.shared.exceptions import MCPError
    from mcp_types import REQUEST_TIMEOUT

    restarts = retries = 0
    snapshot, cursor, rows, seen = None, None, [], set()
    while True:
        requesting_page = False
        try:
            async with connect() as client:
                await _require_tool(client, tool)
                while True:
                    params = {"snapshot_pt": snapshot, "page_size": page_size, "cursor": cursor}
                    requesting_page = True
                    try:
                        result = await client.call_tool(tool, params)
                    except MCPError as exc:
                        if (snapshot is not None and exc.code == config.snapshot_expired_rpc_code
                                and restarts < config.snapshot_restarts):
                            restarts += 1
                            logger.warning("MCP 已报告固定快照失效，全量重启 %d/%d", restarts, config.snapshot_restarts)
                            snapshot, cursor, rows, seen = None, None, [], set()
                            retries = 0
                            continue
                        # The SDK dispatcher remains usable after a request timeout.
                        if exc.code == REQUEST_TIMEOUT and retries < config.page_retries:
                            await _backoff(config, retries)
                            retries += 1
                            continue
                        raise
                    requesting_page = False
                    snapshot, page, cursor = _validate_page(result, snapshot, seen)
                    rows.extend(page)
                    if cursor is None:
                        return rows, snapshot
                    seen.add(cursor)
                    retries = 0
        except Exception as exc:
            # AnyIO transport failures can emerge on context exit. Reconnect and
            # repeat only the in-flight request, keeping previously validated pages.
            if not requesting_page or retries >= config.page_retries or not _transient(exc):
                raise
            await _backoff(config, retries)
            retries += 1
