"""运行配置。

配置集中放在这里，避免在节点中散落硬编码；阈值、路径、Hive 元数据表名
后续都可以通过环境变量或命令行参数继续扩展。
"""

from dataclasses import dataclass, field
from math import isfinite
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
    api_base: str = os.getenv("SAME_GRAPH_LLM_API_BASE", "")
    api_key: str = os.getenv("SAME_GRAPH_LLM_API_KEY", "")
    model: str = os.getenv("SAME_GRAPH_LLM_MODEL", "gpt-5.5")
    timeout_seconds: int = int(os.getenv("SAME_GRAPH_LLM_TIMEOUT_SECONDS", "60"))
    batch_size: int = int(os.getenv("SAME_GRAPH_LLM_BATCH_SIZE", "20"))


SIMILARITY_CONFIG = SimilarityConfig()
RUNTIME_CONFIG = RuntimeConfig()
LLM_REVIEW_CONFIG = LLMReviewConfig()


def _env_number(name, parse, default):
    value = os.getenv(name)
    if value is None or (default is None and not value.strip()):
        return default
    try:
        return parse(value)
    except ValueError:
        raise ValueError(f"{name} 需要有效的 {parse.__name__} 配置值") from None


@dataclass(frozen=True)
class MCPConfig:
    server_url: str
    tool_name: str
    auth_token: str = field(repr=False)
    timeout_seconds: float
    page_size: int
    page_retries: int = 2
    retry_backoff_seconds: float = 1.0
    snapshot_restarts: int = 1
    snapshot_expired_rpc_code: int | None = None

    def __post_init__(self):
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("MCP_TIMEOUT_SECONDS 必须是有限正数")
        if self.page_size <= 0:
            raise ValueError("MCP_PAGE_SIZE 必须大于零")
        if self.page_retries < 0:
            raise ValueError("MCP_PAGE_RETRIES 不能为负数")
        if not isfinite(self.retry_backoff_seconds) or self.retry_backoff_seconds < 0:
            raise ValueError("MCP_RETRY_BACKOFF_SECONDS 必须是有限非负数")
        if self.snapshot_restarts < 0:
            raise ValueError("MCP_SNAPSHOT_RESTARTS 不能为负数")
        code = self.snapshot_expired_rpc_code
        # SDK 2.x 在实现自定义区内占用 -32000、-32001，-32002 已退役。
        if code is not None and (
            code == -32800 or (-32768 <= code <= -32000 and not -32019 <= code <= -32003)
        ):
            raise ValueError("MCP_SNAPSHOT_EXPIRED_RPC_CODE 不能使用协议或 SDK 保留码")

    @classmethod
    def from_env(cls):
        return cls(
            server_url=os.getenv("MCP_SERVER_URL", "").strip(),
            tool_name=os.getenv("MCP_TOOL_NAME", "query_field_metadata").strip(),
            auth_token=os.getenv("MCP_AUTH_TOKEN", ""),
            timeout_seconds=_env_number("MCP_TIMEOUT_SECONDS", float, 60.0),
            page_size=_env_number("MCP_PAGE_SIZE", int, 1000),
            page_retries=_env_number("MCP_PAGE_RETRIES", int, cls.page_retries),
            retry_backoff_seconds=_env_number("MCP_RETRY_BACKOFF_SECONDS", float, cls.retry_backoff_seconds),
            snapshot_restarts=_env_number("MCP_SNAPSHOT_RESTARTS", int, cls.snapshot_restarts),
            snapshot_expired_rpc_code=_env_number("MCP_SNAPSHOT_EXPIRED_RPC_CODE", int, None),
        )


@dataclass(frozen=True)
class MetadataQualityConfig:
    history_path: Path = PROJECT_ROOT / "metadata_quality" / "history.json"
    max_drop_fraction: float = 0.30
    window_size: int = 7

    @classmethod
    def from_env(cls):
        raw_path = os.getenv("METADATA_QUALITY_HISTORY_PATH", str(cls.history_path))
        if not raw_path.strip():
            raise ValueError("METADATA_QUALITY_HISTORY_PATH 需要有效的文件路径")
        path = Path(raw_path).expanduser()
        if path.name.casefold() == "similarity_memory.jsonl":
            raise ValueError("METADATA_QUALITY_HISTORY_PATH 必须独立于相似检索 Memory")
        threshold = _env_number("METADATA_QUALITY_MAX_DROP_FRACTION", float, cls.max_drop_fraction)
        if not 0 <= threshold <= 1:
            raise ValueError("METADATA_QUALITY_MAX_DROP_FRACTION 必须在 0 至 1 之间")
        return cls(history_path=path, max_drop_fraction=threshold)
