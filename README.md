# table-column-same-graph

`table-column-same-graph` 是一个用于数仓相似数据表/字段识别的 LangGraph。它支持开发前输入表名、表中文名、DDL、字段名或字段中文名，自动从 Hive 元数据表中查找已存在的相似资产，并输出 CSV，便于治理烟囱表、重复指标和口径不一致问题。

## 支持数据源

- Hive SQL/表：默认读取 `jdbc:hive2://180.184.31.191:10000`
- 元数据表：`zz.dwd_meta_table_columns_detail_df`
- 离线 CSV：用于 demo、测试或从 Hive 导出后本地运行

## 图流程

1. `prepare_runtime`：补齐输出目录、记忆目录和 Hive 默认配置。
2. `parse_user_input`：识别用户输入意图，解析表名、字段名或 DDL。
3. `load_metadata`：从 Hive 或 CSV 读取字段粒度元数据。
4. `load_memory`：读取历史相似检索记忆。
5. `match_*`：按意图进入单表、全表、单字段或全字段匹配分支。
6. `llm_review`：模型对算法候选做二次复判，只有模型判断相似才进入输出。
7. `write_csv`：按规范输出 CSV 文件。
8. `persist_memory`：压缩沉淀本次检索核心词、模型判断和 Top 命中。

## 输入规范

表相似：

```bash
表 dwd_trade_order_pay_detail_di
表 dwd-交易域-订单域-订单支付明细-增量表
表 CREATE TABLE `zz`.`dwd_trade_order_pay_detail_di`(...)
全表比对
```

字段/指标相似：

```bash
字段 pay_amt_30d
字段 支付金额
字段 pay_amt_30d DECIMAL(18,2) COMMENT '支付金额'
全字段比对
全指标比对
```

如果输入不符合规范，默认按“数据表相似”处理。

## 匹配规则

- 表名/表中文名相似度阈值：`60%`
- DDL 字段一致阈值：单字段匹配 `60%`，源表字段覆盖率 `60%`
- 字段/指标相似度阈值：`75%`
- 英文字段名会拆分下划线，并去掉 `1d/14d/30d/60d/90d` 等窗口尾缀
- 全量比对使用组合去重，避免 `A -> B` 和 `B -> A` 重复输出
- 输出前必须经过模型二次复判；算法负责召回，模型负责最终判断治理意义上是否相似

## 模型复判

默认 `llm_review_mode=required`，表示没有模型配置或模型调用失败时不会输出 CSV，避免生成未经复判的治理结论。

支持 OpenAI-compatible chat completions 接口，可通过环境变量配置：

```bash
export SAME_GRAPH_LLM_API_BASE="https://api.openai.com/v1"
export SAME_GRAPH_LLM_API_KEY="你的 API Key"
export SAME_GRAPH_LLM_MODEL="gpt-4o-mini"
```

也可通过 CLI 参数传入：

```bash
PYTHONPATH=src python -m table_column_same_graph.cli \
  --query "表 dwd_trade_order_pay_detail_di" \
  --llm-api-base "https://api.openai.com/v1" \
  --llm-api-key "$SAME_GRAPH_LLM_API_KEY" \
  --llm-model "gpt-4o-mini"
```

复判模式：

- `required`：默认模式，必须模型判断相似后才输出
- `optional`：模型失败时保留算法结果，并写入 warning，适合调试
- `disabled`：跳过模型复判，仅用于单元测试或离线算法 demo

## 输出 CSV

单表非 DDL 查询：

```text
原表,原表中文名,相似表名,相似表中文名,相似度
```

DDL 单表查询和全表比对：

```text
原表,原表中文名,相似表名,相似表中文名,全表字段相似度
```

单字段和全字段/指标比对：

```text
原字段名,原表名,相似字段名,相似表名,相似度
```

文件名格式：

```text
原表_yyyy-mm-dd hh:mi_csv.csv
原字段_yyyy-mm-dd hh:mi_csv.csv
全表_yyyy-mm-dd hh:mi_csv.csv
全字段_yyyy-mm-dd hh:mi_csv.csv
```

## 安装和运行

```bash
cd /Users/admin/Desktop/codex/test2/table-column-same-graph
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

依赖说明：当前本机 Python 是 `3.9.x`，项目将 `langgraph` 约束在 `>=0.2,<0.3`，避免新版 LangGraph 依赖链在 Python 3.9 下长时间回溯。

Hive 线上运行：

```bash
python -m table_column_same_graph.cli --query "表 dwd_trade_order_pay_detail_di"
```

CSV demo：

```bash
PYTHONPATH=src python -m table_column_same_graph.cli \
  --query "表 dwd_trade_order_pay_detail_di" \
  --source csv \
  --metadata-csv examples/sample_metadata.csv \
  --llm-review-mode disabled
```

全字段比对 demo：

```bash
PYTHONPATH=src python -m table_column_same_graph.cli \
  --query "全字段比对" \
  --source csv \
  --metadata-csv examples/sample_metadata.csv \
  --llm-review-mode disabled
```

## 记忆沉淀

每次运行都会追加写入：

```text
memory/similarity_memory.jsonl
```

沉淀内容包括：

- 输入意图
- 用户输入
- 源表/源字段核心词
- 模型复判摘要和简短理由
- Top 命中的相似表或字段
- CSV 输出路径

下一次单表或单字段检索时，如果当前输入核心词与历史输入核心词重合度达到 `60%`，历史命中过的对象会获得轻微排序提权，用于提升持续复用场景下的命中稳定性。
