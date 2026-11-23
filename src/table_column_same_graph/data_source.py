"""通过 MCP 加载并转换字段粒度元数据。"""
from table_column_same_graph.config import MetadataQualityConfig
from table_column_same_graph.mcp_client import fetch_metadata_pages
from table_column_same_graph.metadata_quality import validate_snapshot
from table_column_same_graph.models import MetadataRow


async def load_metadata_rows() -> tuple[list[MetadataRow], str]:
    rows, snapshot = await fetch_metadata_pages()
    fields = ("database_name", "table_name", "table_comment", "column_name", "column_type", "column_comment")
    converted = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or any(key not in row or not isinstance(row[key], str) for key in fields):
            raise ValueError(f"MCP 元数据第 {i + 1} 行缺少必需字符串字段")
        if not all(row[key].strip() for key in ("database_name", "table_name", "column_name")):
            raise ValueError(f"MCP 元数据第 {i + 1} 行的库名、表名或字段名为空")
        converted.append(MetadataRow(**{key: row[key].strip() for key in fields}))
    validated = validate_snapshot(converted, snapshot, MetadataQualityConfig.from_env())
    return validated, snapshot
