"""LangGraph 编排。

图节点严格对应用户给出的流程图：
意图识别 -> 元数据读取 -> 表/字段分支匹配 -> 模型复判 -> CSV 输出 -> 记忆沉淀。
"""

from typing import Dict, List

from langgraph.graph import END, StateGraph

from table_column_same_graph.config import LLM_REVIEW_CONFIG, RUNTIME_CONFIG
from table_column_same_graph.data_source import load_metadata_rows
from table_column_same_graph.llm_review import review_matches_with_llm
from table_column_same_graph.memory import load_memory_records, persist_memory
from table_column_same_graph.models import ColumnMatch, GraphState, TableMatch
from table_column_same_graph.output import write_matches_to_csv
from table_column_same_graph.parser import parse_column_input, parse_table_input, validate_input
from table_column_same_graph.similarity import (
    find_all_column_matches,
    find_all_table_matches,
    find_single_column_matches,
    find_single_table_matches,
    summarize_column_terms,
    summarize_table_terms,
)
from table_column_same_graph.text_utils import token_overlap_score


def build_graph():
    """构建并编译 LangGraph。

    每个节点只负责一个清晰阶段，便于后续把“相似度算法”替换成向量召回、
    把“输出 CSV”替换成飞书/治理平台写入等能力。
    """

    graph = StateGraph(GraphState)
    graph.add_node("prepare_runtime", prepare_runtime_node)
    graph.add_node("parse_user_input", parse_user_input_node)
    graph.add_node("load_metadata", load_metadata_node)
    graph.add_node("load_memory", load_memory_node)
    graph.add_node("match_single_table", match_single_table_node)
    graph.add_node("match_all_tables", match_all_tables_node)
    graph.add_node("match_single_column", match_single_column_node)
    graph.add_node("match_all_columns", match_all_columns_node)
    graph.add_node("llm_review", llm_review_node)
    graph.add_node("persist_memory", persist_memory_node)
    graph.add_node("write_csv", write_csv_node)

    graph.set_entry_point("prepare_runtime")
    graph.add_edge("prepare_runtime", "parse_user_input")
    graph.add_conditional_edges("parse_user_input", route_by_validation, {"VALID": "load_metadata", "INVALID": END})
    graph.add_edge("load_metadata", "load_memory")
    graph.add_conditional_edges(
        "load_memory",
        route_by_intent,
        {
            "single_table": "match_single_table",
            "all_tables": "match_all_tables",
            "single_column": "match_single_column",
            "all_columns": "match_all_columns",
        },
    )
    graph.add_edge("match_single_table", "llm_review")
    graph.add_edge("match_all_tables", "llm_review")
    graph.add_edge("match_single_column", "llm_review")
    graph.add_edge("match_all_columns", "llm_review")
    graph.add_edge("llm_review", "write_csv")
    graph.add_edge("write_csv", "persist_memory")
    graph.add_edge("persist_memory", END)
    return graph.compile()


def prepare_runtime_node(state: GraphState) -> Dict:
    """补齐运行默认值。

    CLI 可以只传 user_input；目录和模型配置在这里统一补齐。
    """

    return {
        "output_dir": state.get("output_dir") or str(RUNTIME_CONFIG.output_dir),
        "memory_dir": state.get("memory_dir") or str(RUNTIME_CONFIG.memory_dir),
        "llm_review_mode": state.get("llm_review_mode") or LLM_REVIEW_CONFIG.review_mode,
        "llm_api_base": state.get("llm_api_base") or LLM_REVIEW_CONFIG.api_base,
        "llm_api_key": state.get("llm_api_key") or LLM_REVIEW_CONFIG.api_key,
        "llm_model": state.get("llm_model") or LLM_REVIEW_CONFIG.model,
        "llm_timeout_seconds": state.get("llm_timeout_seconds") or LLM_REVIEW_CONFIG.timeout_seconds,
        "llm_batch_size": state.get("llm_batch_size") or LLM_REVIEW_CONFIG.batch_size,
        "warnings": state.get("warnings", []),
    }


def parse_user_input_node(state: GraphState) -> Dict:
    """解析用户输入为意图、源表或源字段。"""

    user_input = state.get("user_input", "")
    result = validate_input(user_input)
    # Preserve the original all-scope source terms used by memory summaries.
    if result.get("intent") == "all_tables":
        result["source_table"] = parse_table_input(user_input)
    elif result.get("intent") == "all_columns":
        result["source_column"] = parse_column_input(user_input)
    return result


def route_by_validation(state: GraphState) -> str:
    return state["validation_status"]


async def load_metadata_node(state: GraphState) -> Dict:
    """通过 MCP 读取元数据及快照。"""

    rows, snapshot = await load_metadata_rows()
    return {"metadata_rows": rows, "metadata_snapshot_pt": snapshot}


def load_memory_node(state: GraphState) -> Dict:
    """加载历史相似检索记忆，用于本次结果增强。"""

    records = load_memory_records(state["memory_dir"])
    return {"memory_records": records}


def route_by_intent(state: GraphState) -> str:
    """按意图选择 LangGraph 分支。"""

    return state["intent"]


def match_single_table_node(state: GraphState) -> Dict:
    """单表相似查询分支。"""

    matches = find_single_table_matches(state["source_table"], state["metadata_rows"])
    boosted = boost_table_matches_from_memory(matches, state)
    return {"table_matches": boosted}


def match_all_tables_node(state: GraphState) -> Dict:
    """全表比对分支。"""

    matches = find_all_table_matches(state["metadata_rows"])
    return {"table_matches": matches}


def match_single_column_node(state: GraphState) -> Dict:
    """单字段/指标相似查询分支。"""

    matches = find_single_column_matches(state["source_column"], state["metadata_rows"])
    boosted = boost_column_matches_from_memory(matches, state)
    return {"column_matches": boosted}


def match_all_columns_node(state: GraphState) -> Dict:
    """全字段/全指标比对分支。"""

    matches = find_all_column_matches(state["metadata_rows"])
    return {"column_matches": matches}


def llm_review_node(state: GraphState) -> Dict:
    """在输出 CSV 之前调用模型对算法候选做二次复判。

    required 模式下，模型未配置或调用失败会阻止 CSV 输出；
    这是为了满足“模型判断后才能输出”的治理要求。
    """

    return review_matches_with_llm(state)


def persist_memory_node(state: GraphState) -> Dict:
    """把本次检索核心信息沉淀为 LangGraph 流程记忆。"""

    summary = persist_memory(state)
    return {"memory_summary": summary}


def write_csv_node(state: GraphState) -> Dict:
    """将最终结果写成 CSV 文件。"""

    output_file = write_matches_to_csv(state)
    return {"output_file": output_file}


def boost_table_matches_from_memory(matches: List[TableMatch], state: GraphState) -> List[TableMatch]:
    """用历史记忆增强单表查询结果排序。

    只在当前输入核心词与历史输入核心词重合度 >= 60% 时生效；
    生效后对历史命中过的目标表轻微提权 0.03，避免旧记忆覆盖本次真实相似度。
    """

    source_terms = summarize_table_terms(state["source_table"])
    remembered_targets = _collect_remembered_targets(
        records=state.get("memory_records", []),
        current_intent="single_table",
        current_terms=source_terms,
        target_key="target_table_name",
    )
    if not remembered_targets:
        return matches

    for match in matches:
        if match.target_table_name in remembered_targets:
            if match.field_similarity is not None:
                match.field_similarity = min(1.0, match.field_similarity + 0.03)
            match.name_similarity = min(1.0, match.name_similarity + 0.03)
    return sorted(matches, key=lambda item: (item.field_similarity or item.name_similarity), reverse=True)


def boost_column_matches_from_memory(matches: List[ColumnMatch], state: GraphState) -> List[ColumnMatch]:
    """用历史记忆增强单字段查询结果排序。"""

    source_terms = summarize_column_terms(state["source_column"])
    remembered_targets = _collect_remembered_targets(
        records=state.get("memory_records", []),
        current_intent="single_column",
        current_terms=source_terms,
        target_key="target_column_name",
    )
    if not remembered_targets:
        return matches

    for match in matches:
        if match.target_column_name in remembered_targets:
            match.similarity = min(1.0, match.similarity + 0.03)
    return sorted(matches, key=lambda item: item.similarity, reverse=True)


def _collect_remembered_targets(
    records: List[Dict],
    current_intent: str,
    current_terms: Dict[str, List[str]],
    target_key: str,
) -> set:
    """从历史记忆中提取可增强的目标对象名。

    记忆匹配既看英文核心词，也看中文核心词；只要任一侧覆盖率达到 60%，认为场景相近。
    """

    current_name_terms = current_terms.get("name_terms", [])
    current_comment_terms = current_terms.get("comment_terms", [])
    targets = set()
    for record in records:
        if record.get("intent") != current_intent:
            continue
        source_terms = record.get("source_terms", {})
        name_score = token_overlap_score(current_name_terms, source_terms.get("name_terms", []))
        comment_score = token_overlap_score(current_comment_terms, source_terms.get("comment_terms", []))
        if max(name_score, comment_score) < 0.60:
            continue
        for match in record.get("top_matches", []):
            target = match.get(target_key)
            if target:
                targets.add(target)
    return targets
