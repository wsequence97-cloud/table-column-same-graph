# 相似表与字段识别 V1

LangGraph 通过项目内部的官方 MCP Python SDK Client 连接数据平台已有的 MCP Server，读取字段级元数据；规则算法召回候选，大模型复判，输出 CSV 并记录本地 JSONL 记忆。项目不托管 MCP Server。实际平台连通性尚未验证。

## MCP 工具契约

默认工具 `query_field_metadata` 接受 `{"snapshot_pt": null, "page_size": 1000, "cursor": null}`。平台负责选择 `zz.dwd_meta_table_columns_detail_df` 的最新有效快照、过滤 `is_del=0`、鉴权并分页。响应示例为 `{"snapshot_pt":"20261123","rows":[],"next_cursor":null}`。后续请求沿用第一页返回的快照和服务端返回的游标。单页可以为空，只要有下一页就继续读取。只有读到 `next_cursor=null` 才检查整份快照。

明确的超时或临时连接故障仅重试当前页。默认额外请求两次，分别等待 1 秒、2 秒，重试耗尽即终止。权限、工具、参数、响应结构、快照不一致和游标异常均会阻断读取。客户端保留调用的取消语义。

快照恢复需要平台约定专用 JSON-RPC 错误码。默认没有映射，工具错误文本（包括“快照已过期”）会终止任务。显式配置约定的错误码后，读取固定快照期间收到该码会丢弃已收集的行和游标，从 `snapshot_pt=null`、`cursor=null` 重新获取最新快照，默认最多重启一次。平台的游标重放稳定性和预期总行数尚待确认。客户端可以检查分页协议和历史数量，无法独立证明平台已返回所有原始行。

## 元数据进入匹配的条件

六个基础字段 `database_name`、`table_name`、`table_comment`、`column_name`、`column_type`、`column_comment` 必须都是字符串。去除首尾空白后，库名、表名、字段名必须非空。表注释、字段注释或字段类型为空时保留资产，记录质量告警。完整快照为空会终止本次查询。

同一字段由 `(database_name, table_name, column_name)` 标识。六项完全一致时保留第一条，同一字段的类型或注释冲突、同一张表的表注释冲突均会终止任务。库名参与表计数，不同库的同名表分别计数。

成功记录默认保存在 `metadata_quality/history.json`。每份快照只保存统计值和带时区的校验时间。记录包括原始行数、去重后字段数、去重表数、重复行数，以及空表注释、空字段注释、空字段类型的字段行数。后三项均按去重后的字段行计数。快照复读时核对全部计数，统计不一致即报错。校验成功后立即保存，所以后续 LLM 或 CSV 即使失败，该次元数据仍可用作历史参考。

对于日期快照，向前取最近 7 个不同快照日期的成功记录，分别计算字段数和表数的中位数。任一指标比对应中位数下降**超过**阈值就阻断，恰好下降 30% 在默认阈值下允许通过。历史不足 7 个日期时记录初始化或积累进度，只执行其余质量检查。`YYYYMMDD` 与 `YYYY-MM-DD` 按同一日处理。无法确定日期的快照 ID 仍做复读一致性检查，有足够日期历史时也要接受数量检查，但它本身不计入七日窗口。

质量历史使用同目录文件锁保护读、比较和写入，并用临时文件原子替换。运行进程应共享同一个支持文件锁和原子替换的持久目录。锁竞争、文件损坏或写入失败会中止本次查询。锁竞争可在其他写入结束后重试，损坏的历史应从可信备份恢复。元数据读取或质量校验失败时，图在 `load_metadata` 结束，相似检索 Memory、匹配、LLM 和 CSV 均不会执行。

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

| 可靠性与质量配置 | 默认值 | 用途 |
| --- | --- | --- |
| `MCP_PAGE_RETRIES` | `2` | 单页额外尝试次数，0 表示仅请求一次 |
| `MCP_RETRY_BACKOFF_SECONDS` | `1` | 首次退避秒数，以后每次加倍 |
| `MCP_SNAPSHOT_RESTARTS` | `1` | 明确快照失效后的额外全量读取次数 |
| `MCP_SNAPSHOT_EXPIRED_RPC_CODE` | 未设置 | 只接受平台专用于快照失效的整数码，禁止复用 SDK 超时、关闭连接、协议参数错误等保留码 |
| `METADATA_QUALITY_HISTORY_PATH` | 项目下的 `metadata_quality/history.json` | 质量历史文件路径，与相似检索 Memory 独立 |
| `METADATA_QUALITY_MAX_DROP_FRACTION` | `0.30` | 字段数和表数各自允许的最大相对降幅，范围为 0 至 1 |

## 启动与测试

```bash
python -m table_column_same_graph.cli --query "表 dwd_trade_order_pay_detail_di"
python -m pytest -q
```

支持 `全表比对`、`字段 pay_amt`、`全字段比对` 等输入。默认必须经过模型复判；`--llm-review-mode disabled` 只用于调试。测试中的 MCP 使用 mock，不代表已连接真实数据平台。

输入采用白名单：`表 表名`、`表 中文描述`、`字段 字段名`、`字段 中文描述`、带类型的字段定义、完整的 `CREATE TABLE` DDL，以及完整口令 `全表比对`、`全字段比对`、`全指标比对`。前缀也可使用冒号，例如 `表：表名`。不符合格式、缺少对象或 DDL 不完整时返回错误提示并以退出码 2 结束，不读取 MCP 元数据，也不生成输出或记忆。未匹配到候选属于正常查询结果。
