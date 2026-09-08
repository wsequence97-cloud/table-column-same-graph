"""运行配置。

配置集中放在这里，避免在节点中散落硬编码；阈值、路径、Hive 元数据表名
后续都可以通过环境变量或命令行参数继续扩展。
"""

from dataclasses import dataclass
from pathlib import Path
import os


# 项目根目录：src/table_column_same_graph/config.py 向上三层即项目根。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SimilarityConfig:
    """相似度阈值配置。"""

    # 表名/表中文名进入候选的阈值，用户需求要求大于 60%。
    table_name_threshold: float = 0.60
    # DDL 字段逐字段匹配阈值，用户需求要求满足 60% 才认为字段一致。
    table_column_threshold: float = 0.60
    # 字段/指标单字段相似阈值，用户需求要求满足 75%。
    column_threshold: float = 0.75
    # 全表比对时字段集合相似阈值，延续表字段一致规则。
    all_table_field_threshold: float = 0.60
    # 全字段/指标比对时字段相似阈值，延续单字段规则。
    all_column_threshold: float = 0.75


@dataclass(frozen=True)
class HiveConfig:
    """Hive 元数据读取配置。"""

    # 用户指定的 JDBC 连接串，用于 README 和状态记录。
    jdbc_url: str = "jdbc:hive2://180.184.31.191:10000"
    # PyHive 实际需要 host 和 port，不直接消费 JDBC 字符串。
    host: str = "180.184.31.191"
    port: int = 10000
    # 元数据表固定为用户提供的 Hive 表。
    database: str = "zz"
    table: str = "dwd_meta_table_columns_detail_df"
    # 默认读取最新分区，SQL 会通过 max(pt) 获取。
    partition_column: str = "pt"


@dataclass(frozen=True)
class RuntimeConfig:
    """默认运行目录配置。"""

    # CSV 输出目录。
    output_dir: Path = PROJECT_ROOT / "outputs"
    # 相似检索记忆沉淀目录。
    memory_dir: Path = PROJECT_ROOT / "memory"


@dataclass(frozen=True)
class LLMReviewConfig:
    """模型复判默认配置。

    不在代码里硬编码具体 API Key；线上运行时通过环境变量或 CLI 参数注入。
    """

    # 默认 required，符合“输出前必须由模型判断”的治理要求。
    review_mode: str = os.getenv("SAME_GRAPH_LLM_REVIEW_MODE", "required")
    # 兼容 OpenAI 或企业内部 OpenAI-compatible 网关。
    api_base: str = os.getenv("SAME_GRAPH_LLM_API_BASE", "https://way.ydata.vip/v1")
    api_key: str = os.getenv("SAME_GRAPH_LLM_API_KEY", "sk-xluhLSmOi9MsoAPOQrVrsexxz2BoTjLZ2PSqyFM8ExAUpnAj")
    model: str = os.getenv("SAME_GRAPH_LLM_MODEL", "gpt-5.5")
    timeout_seconds: int = int(os.getenv("SAME_GRAPH_LLM_TIMEOUT_SECONDS", "60"))
    batch_size: int = int(os.getenv("SAME_GRAPH_LLM_BATCH_SIZE", "20"))


SIMILARITY_CONFIG = SimilarityConfig()
HIVE_CONFIG = HiveConfig()
RUNTIME_CONFIG = RuntimeConfig()
LLM_REVIEW_CONFIG = LLMReviewConfig()
