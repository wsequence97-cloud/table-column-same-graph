"""相似检索记忆沉淀。

每次运行后把输入、核心词、命中结果压缩为 JSONL 记录。
后续可以把这些记录接入向量库或 LangGraph checkpoint，目前先用本地可读文件
保证交付物独立、可审计。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from table_column_same_graph.models import ColumnMatch, GraphState, TableMatch
from table_column_same_graph.similarity import summarize_column_terms, summarize_table_terms


def persist_memory(state: GraphState) -> Dict[str, Any]:
    """写入本次检索的压缩记忆，并返回摘要。"""

    memory_dir = Path(state["memory_dir"]).expanduser().resolve()
    memory_dir.mkdir(parents=True, exist_ok=True)

    summary = _build_memory_summary(state)
    memory_file = memory_dir / "similarity_memory.jsonl"
    with memory_file.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


def load_memory_records(memory_dir: str) -> List[Dict[str, Any]]:
    """读取历史相似检索记忆。

    记忆文件采用 JSONL 追加写；读取时跳过损坏行，避免单条异常影响主流程。
    """

    path = Path(memory_dir).expanduser().resolve() / "similarity_memory.jsonl"
    if not path.exists():
        return []

    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-200:]


def _build_memory_summary(state: GraphState) -> Dict[str, Any]:
    """生成记忆摘要。

    只沉淀治理复用需要的核心信息，不把完整元数据表写入记忆，避免文件膨胀。
    """

    intent = state["intent"]
    base: Dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "intent": intent,
        "user_input": state.get("user_input", ""),
        "output_file": state.get("output_file", ""),
        "warnings": state.get("warnings", []),
        "llm_review_summary": state.get("llm_review_summary", {}),
    }

    if intent in {"single_table", "all_tables"}:
        source_table = state.get("source_table")
        if source_table:
            base["source_terms"] = summarize_table_terms(source_table)
        base["top_matches"] = _summarize_table_matches(state.get("table_matches", []))
    else:
        source_column = state.get("source_column")
        if source_column:
            base["source_terms"] = summarize_column_terms(source_column)
        base["top_matches"] = _summarize_column_matches(state.get("column_matches", []))
    return base


def _summarize_table_matches(matches: List[TableMatch]) -> List[Dict[str, Any]]:
    """压缩表匹配结果，只保留前 20 条核心信息。"""

    return [
        {
            "target_table_name": match.target_table_name,
            "target_table_comment": match.target_table_comment,
            "name_similarity": round(match.name_similarity, 4),
            "field_similarity": None if match.field_similarity is None else round(match.field_similarity, 4),
            "llm_reason": match.llm_reason,
        }
        for match in matches[:20]
    ]


def _summarize_column_matches(matches: List[ColumnMatch]) -> List[Dict[str, Any]]:
    """压缩字段匹配结果，只保留前 20 条核心信息。"""

    return [
        {
            "target_column_name": match.target_column_name,
            "target_table_name": match.target_table_name,
            "similarity": round(match.similarity, 4),
            "llm_reason": match.llm_reason,
        }
        for match in matches[:20]
    ]
