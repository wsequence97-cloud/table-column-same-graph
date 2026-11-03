"""项目内通用数据结构定义。

这些 dataclass 只表达业务对象本身，不包含 Hive、CSV 或 LangGraph 的细节，
这样解析、相似度计算、输出节点之间可以稳定传递结构化信息。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, TypedDict


# 用户意图类型：表单次查询、字段单次查询、全表比对、全字段/指标比对。
Intent = Literal["single_table", "single_column", "all_tables", "all_columns"]


@dataclass
class ColumnInfo:
    """字段元数据。

    table_name/table_comment 可为空：用户只输入一个字段时，通常没有所属表信息；
    从 Hive 元数据读取出的字段会补齐所属表，便于输出相似字段所在表。
    """

    column_name: str = ""
    column_type: str = ""
    column_comment: str = ""
    table_name: str = ""
    table_comment: str = ""


@dataclass
class TableInfo:
    """表元数据。

    columns 用于 DDL 场景的字段级二阶段匹配；如果用户只输入表名或中文名，
    columns 为空，流程会只做表名/表中文名相似输出。
    """

    table_name: str = ""
    table_comment: str = ""
    database_name: str = ""
    columns: List[ColumnInfo] = field(default_factory=list)


@dataclass
class MetadataRow:
    """Hive 元数据明细表的一行。

    zz.dwd_meta_table_columns_detail_df 是字段粒度，所以每行对应某张表的某个字段。
    table_comment 字段兼容历史表里的 table_coment 拼写。
    """

    database_name: str
    table_name: str
    table_comment: str
    column_name: str
    column_type: str
    column_comment: str


@dataclass
class TableMatch:
    """表相似结果。

    name_similarity 表示表名/表中文名第一阶段相似度；
    field_similarity 表示 DDL 或全表比对时字段集合相似度。
    """

    source_table_name: str
    source_table_comment: str
    target_table_name: str
    target_table_comment: str
    name_similarity: float = 0.0
    field_similarity: Optional[float] = None
    # 模型二次复判结果；只有 True 的候选才会进入最终 CSV。
    llm_is_similar: Optional[bool] = None
    # 模型给出的简短判断理由，默认不写入 CSV，但会沉淀到 memory 供治理复盘。
    llm_reason: str = ""


@dataclass
class ColumnMatch:
    """字段或指标相似结果。"""

    source_column_name: str
    source_table_name: str
    target_column_name: str
    target_table_name: str
    similarity: float
    # 模型二次复判结果；只有 True 的候选才会进入最终 CSV。
    llm_is_similar: Optional[bool] = None
    # 模型给出的简短判断理由，默认不写入 CSV，但会沉淀到 memory 供治理复盘。
    llm_reason: str = ""


class GraphState(TypedDict, total=False):
    """LangGraph 节点之间共享的状态。

    使用 TypedDict 是为了让每个节点只读写自己关心的键，同时保持图状态可序列化。
    """

    # 用户原始输入，一般来自 CLI 的 --query。
    user_input: str
    # 输出目录，默认写到项目 outputs 下。
    output_dir: str
    # 记忆沉淀目录，默认写到项目 memory 下。
    memory_dir: str
    # 解析出的意图。
    intent: Intent
    # 原始输入是否包含 create table/create view DDL。
    has_ddl: bool
    # 解析出的源表信息。
    source_table: TableInfo
    # 解析出的源字段信息。
    source_column: ColumnInfo
    # 从数据源加载出的字段粒度元数据。
    metadata_rows: List[MetadataRow]
    metadata_snapshot_pt: str
    # 表相似候选或最终结果。
    table_matches: List[TableMatch]
    # 字段相似结果。
    column_matches: List[ColumnMatch]
    # CSV 输出文件路径。
    output_file: str
    # 每次运行后写入记忆的摘要信息。
    memory_summary: Dict[str, Any]
    # 过去运行沉淀的 JSONL 记忆，匹配节点会用它做轻量结果增强。
    memory_records: List[Dict[str, Any]]
    # 运行中的提示或降级信息，例如 Hive 不可用时的错误。
    warnings: List[str]
    # 模型复判模式：required=必须调用模型，optional=失败时保留算法结果，disabled=跳过模型复判。
    llm_review_mode: str
    # OpenAI-compatible chat completions 地址，例如 https://api.openai.com/v1。
    llm_api_base: str
    # 模型 API Key；本地模型服务无鉴权时可为空。
    llm_api_key: str
    # 模型名称；required/optional 模式下必须配置。
    llm_model: str
    # 模型请求超时时间。
    llm_timeout_seconds: int
    # 每批交给模型复判的候选数量，控制 prompt 长度和单次调用成本。
    llm_batch_size: int
    # 模型复判摘要，记录保留/过滤数量和降级原因。
    llm_review_summary: Dict[str, Any]
