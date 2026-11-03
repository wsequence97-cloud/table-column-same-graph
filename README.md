# 相似表与字段识别 V1

LangGraph 通过项目内部的官方 MCP Python SDK Client 连接数据平台已有的 MCP Server，读取字段级元数据；规则算法召回候选，大模型复判，输出 CSV 并记录本地 JSONL 记忆。项目不托管 MCP Server。实际平台连通性尚未验证。

## MCP 工具契约

默认工具 `query_field_metadata` 接受 `{"snapshot_pt": null, "page_size": 1000, "cursor": null}`。平台负责获取最新快照、过滤删除项并分页返回 `{"snapshot_pt":"2026-11-03","rows":[],"next_cursor":null}`。每行必须包含字符串字段 `database_name`、`table_name`、`table_comment`、`column_name`、`column_type`、`column_comment`。后续分页固定第一页返回的快照；异常、缺字段、快照变化和重复游标均中止任务。目标资产为 `zz.dwd_meta_table_columns_detail_df`。

## 安装和配置

需要 Python 3.11+：

```powershell
python -m venv .venv
./.venv/Scripts/Activate.ps1
pip install -r requirements-dev.txt
pip install -e .
```

Linux/macOS 可使用 `source .venv/bin/activate` 激活。激活后配置：

- `MCP_SERVER_URL`：平台 Streamable HTTP MCP 地址，必填。
- `MCP_TOOL_NAME`：工具名，默认 `query_field_metadata`。
- `MCP_AUTH_TOKEN`：Bearer token，可选，按平台要求配置。
- `MCP_TIMEOUT_SECONDS`：超时秒数，默认 60。
- `MCP_PAGE_SIZE`：分页大小，默认 1000。
- `SAME_GRAPH_LLM_API_BASE`、`SAME_GRAPH_LLM_API_KEY`、`SAME_GRAPH_LLM_MODEL`：模型接口配置。

## 启动与测试

```bash
python -m table_column_same_graph.cli --query "表 dwd_trade_order_pay_detail_di"
python -m pytest -q
```

支持 `全表比对`、`字段 pay_amt`、`全字段比对` 等输入。默认必须经过模型复判；`--llm-review-mode disabled` 只用于调试。测试中的 MCP 使用 mock，不代表已连接真实数据平台。
