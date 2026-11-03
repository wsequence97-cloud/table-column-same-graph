"""使用官方 MCP SDK 读取同一快照的全部分页。"""
from table_column_same_graph.config import MCPConfig

async def fetch_metadata_pages():
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    config = MCPConfig.from_env()
    url, tool, token = config.server_url, config.tool_name, config.auth_token
    timeout, page_size = config.timeout_seconds, config.page_size
    if not url or not tool or timeout <= 0 or page_size <= 0:
        raise ValueError("请配置有效的 MCP_SERVER_URL、MCP_TOOL_NAME、超时和分页大小")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                return await fetch_from_client(client, tool, page_size)
    except Exception as exc:
        raise RuntimeError(f"MCP 元数据读取失败: {exc}") from exc

async def fetch_from_client(client, tool, page_size):
    tool_cursor, seen_tool_cursors = None, set()
    while True:
        listing = await client.list_tools(cursor=tool_cursor)
        if any(item.name == tool for item in listing.tools):
            break
        tool_cursor = listing.next_cursor
        if not tool_cursor:
            raise RuntimeError(f"MCP Server 未提供工具 {tool}")
        if tool_cursor in seen_tool_cursors:
            raise RuntimeError("MCP 工具列表游标重复")
        seen_tool_cursors.add(tool_cursor)
    snapshot, cursor, rows, seen = None, None, [], set()
    while True:
        result = await client.call_tool(tool, {"snapshot_pt": snapshot, "page_size": page_size, "cursor": cursor})
        if result.is_error:
            raise RuntimeError(f"MCP 工具 {tool} 返回错误: {result.content}")
        data = result.structured_content
        if not isinstance(data, dict) or not isinstance(data.get("snapshot_pt"), str) or not data["snapshot_pt"].strip() or not isinstance(data.get("rows"), list) or "next_cursor" not in data:
            raise ValueError("MCP 工具返回的快照、rows 或 next_cursor 格式无效")
        if snapshot is None:
            snapshot = data["snapshot_pt"]
        elif snapshot != data["snapshot_pt"]:
            raise ValueError("MCP 分页返回了不同的快照")
        rows.extend(data["rows"])
        cursor = data["next_cursor"]
        if cursor is None:
            return rows, snapshot
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("MCP 分页游标无效或重复")
        seen.add(cursor)
