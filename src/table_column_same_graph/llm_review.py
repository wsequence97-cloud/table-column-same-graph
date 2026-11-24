"""模型二次复判。

算法相似度负责召回候选，模型复判负责在输出前判断“治理上是否真的相似”。
该模块使用 OpenAI-compatible chat completions HTTP 协议，不强绑定某个 SDK，
方便接入 OpenAI、企业网关或本地兼容服务。
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from itertools import islice
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from table_column_same_graph.models import ColumnInfo, ColumnMatch, GraphState, MetadataRow, TableMatch


_PROMPT_COLUMN_LIMIT = 50


class _ReviewMetadata:
    """每次复判共用的物理表和字段索引。旧候选缺库名时仅解析唯一表名。"""

    def __init__(self, rows: Sequence[MetadataRow]):
        self.tables: dict[tuple[str, str], dict[str, MetadataRow]] = {}
        self.unambiguous_database: dict[str, str] = {}
        for row in rows:
            self.tables.setdefault((row.database_name, row.table_name), {})[row.column_name] = row
        for database, table in self.tables:
            if self.unambiguous_database.setdefault(table, database) != database:
                self.unambiguous_database[table] = ""

    def table_key(self, database: str, table: str) -> tuple[str, str]:
        return database or self.unambiguous_database.get(table, ""), table


@dataclass
class LLMClientConfig:
    """模型客户端配置。"""

    api_base: str
    api_key: str
    model: str
    timeout_seconds: int = 60


class OpenAICompatibleClient:
    """OpenAI-compatible chat completions 客户端。

    只使用 Python 标准库，避免额外引入 SDK 依赖；请求体保持最小集合，提高兼容性。
    """

    def __init__(self, config: LLMClientConfig):
        self.config = config

    def complete_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """请求模型并解析 JSON 对象。

        优先使用 OpenAI Chat Completions；若网关返回无内容的 SSE，则自动降级到
        OpenAI Responses API，兼容只开放 /responses 的模型网关。
        """

        payload = {
            "model": self.config.model,
            "temperature": 0,
            # 部分 OpenAI-compatible 网关在未显式指定 max_tokens 时会返回 completion_tokens=0。
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        request = urllib.request.Request(
            url=_chat_completions_url(self.config.api_base),
            data=request_data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"模型复判 HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"模型复判请求失败: {exc}") from exc

        try:
            content = _extract_chat_content(response_body, content_type)
        except ValueError as chat_exc:
            content = self._complete_responses_text(system_prompt, user_prompt, chat_exc)
        return parse_model_json(content)

    def _complete_responses_text(self, system_prompt: str, user_prompt: str, chat_exc: Exception) -> str:
        """调用 Responses API 并提取文本。

        只在 chat 响应无法提取 content 时使用；如果 responses 也失败，会带上 chat 失败原因。
        """

        payload = {
            "model": self.config.model,
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": 1024,
        }
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        request = urllib.request.Request(
            url=_responses_url(self.config.api_base),
            data=request_data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"chat 无内容且 responses HTTP {exc.code}: {error_body}; chat_error={chat_exc}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"chat 无内容且 responses 请求失败: {exc}; chat_error={chat_exc}") from exc

        try:
            return _extract_responses_content(response_body)
        except Exception as response_exc:
            raise RuntimeError(f"chat 无内容且 responses 无法解析: {response_exc}; chat_error={chat_exc}") from response_exc


def review_matches_with_llm(state: GraphState) -> Dict[str, Any]:
    """对算法候选结果进行模型复判，并返回过滤后的结果。

    required 模式下没有模型配置或模型调用失败会直接抛错，确保不会输出未经复判的结论。
    optional 模式下失败会保留算法候选并写入 warning，便于调试环境继续跑通。
    disabled 模式仅用于本地单测或离线算法验证。
    """

    mode = state.get("llm_review_mode", "required")
    if mode == "disabled":
        return {
            "llm_review_summary": {
                "mode": mode,
                "reviewed": 0,
                "kept": _current_match_count(state),
                "filtered": 0,
                "message": "模型复判已禁用，仅用于测试或离线 demo。",
            }
        }

    try:
        client = _build_client_from_state(state)
        if state["intent"] in {"single_table", "all_tables"}:
            reviewed_matches, summary = _review_table_matches(state, client)
            return {"table_matches": reviewed_matches, "llm_review_summary": summary}
        reviewed_matches, summary = _review_column_matches(state, client)
        return {"column_matches": reviewed_matches, "llm_review_summary": summary}
    except Exception as exc:
        if mode == "optional":
            warnings = list(state.get("warnings", []))
            warnings.append(f"模型复判失败，已保留算法结果：{exc}")
            return {
                "warnings": warnings,
                "llm_review_summary": {
                    "mode": mode,
                    "reviewed": 0,
                    "kept": _current_match_count(state),
                    "filtered": 0,
                    "message": str(exc),
                },
            }
        raise


def parse_model_json(content: str) -> Dict[str, Any]:
    """解析模型返回的 JSON。

    兼容模型把 JSON 包在 ```json 代码块里的情况；如果仍无法解析，会抛出明确错误。
    """

    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        object_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not object_match:
            raise
        parsed = json.loads(object_match.group(0))
    if isinstance(parsed, list):
        return {"decisions": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("模型复判结果必须是 JSON object 或 JSON array")
    return parsed


def _extract_chat_content(response_body: str, content_type: str) -> str:
    """从普通 JSON 或 SSE 响应中提取模型文本。

    某些 OpenAI-compatible 网关即使未显式开启 stream，也会返回 text/event-stream；
    这里兼容两种格式，保证复判节点可以稳定解析模型输出。
    """

    if "text/event-stream" in (content_type or "").lower() or response_body.lstrip().startswith("data:"):
        return _extract_sse_chat_content(response_body)

    raw_json = json.loads(response_body)
    choices = raw_json.get("choices", [])
    if not choices:
        raise ValueError(f"模型响应 JSON 中缺少 choices：{response_body[:300]}")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        raise ValueError(f"模型响应 JSON 中 message.content 为空：{response_body[:300]}")
    return content


def _extract_sse_chat_content(response_body: str) -> str:
    """从 OpenAI 风格 SSE data 行中拼接 content。"""

    parts: List[str] = []
    last_payload = ""
    for raw_line in response_body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        last_payload = data
        payload = json.loads(data)
        for choice in payload.get("choices", []):
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            content = delta.get("content") or message.get("content") or ""
            if content:
                parts.append(content)

    content = "".join(parts).strip()
    if not content:
        raise ValueError(f"模型 SSE 响应中未提取到 content，最后一个 payload：{last_payload[:300]}")
    return content


def apply_table_decisions(matches: Sequence[TableMatch], decisions: Sequence[Dict[str, Any]]) -> List[TableMatch]:
    """按模型决策过滤表相似候选。"""

    decision_map = _decision_map(decisions)
    kept: List[TableMatch] = []
    for index, match in enumerate(matches):
        decision = decision_map.get(index, {"is_similar": False, "reason": "模型未返回该候选决策"})
        match.llm_is_similar = bool(decision.get("is_similar"))
        match.llm_reason = str(decision.get("reason") or "")
        if match.llm_is_similar:
            kept.append(match)
    return kept


def apply_column_decisions(matches: Sequence[ColumnMatch], decisions: Sequence[Dict[str, Any]]) -> List[ColumnMatch]:
    """按模型决策过滤字段相似候选。"""

    decision_map = _decision_map(decisions)
    kept: List[ColumnMatch] = []
    for index, match in enumerate(matches):
        decision = decision_map.get(index, {"is_similar": False, "reason": "模型未返回该候选决策"})
        match.llm_is_similar = bool(decision.get("is_similar"))
        match.llm_reason = str(decision.get("reason") or "")
        if match.llm_is_similar:
            kept.append(match)
    return kept


def _review_table_matches(state: GraphState, client: OpenAICompatibleClient) -> Tuple[List[TableMatch], Dict[str, Any]]:
    """批量复判表相似候选。"""

    matches = state.get("table_matches", [])
    if not matches:
        return [], _summary("required", reviewed=0, kept=0, filtered=0)

    batch_size = max(1, int(state.get("llm_batch_size", 20)))
    metadata = _ReviewMetadata(state.get("metadata_rows", []))
    kept: List[TableMatch] = []
    reviewed = 0
    for batch in _batches(matches, batch_size):
        candidates = _table_candidates(batch, metadata, state)
        decisions = _ask_model_for_decisions(client, "table", candidates)
        kept.extend(apply_table_decisions(batch, decisions))
        reviewed += len(batch)
    return kept, _summary(state.get("llm_review_mode", "required"), reviewed, len(kept), reviewed - len(kept))


def _review_column_matches(state: GraphState, client: OpenAICompatibleClient) -> Tuple[List[ColumnMatch], Dict[str, Any]]:
    """批量复判字段/指标相似候选。"""

    matches = state.get("column_matches", [])
    if not matches:
        return [], _summary("required", reviewed=0, kept=0, filtered=0)

    batch_size = max(1, int(state.get("llm_batch_size", 20)))
    metadata = _ReviewMetadata(state.get("metadata_rows", []))
    kept: List[ColumnMatch] = []
    reviewed = 0
    for batch in _batches(matches, batch_size):
        candidates = _column_candidates(batch, metadata, state)
        decisions = _ask_model_for_decisions(client, "column", candidates)
        kept.extend(apply_column_decisions(batch, decisions))
        reviewed += len(batch)
    return kept, _summary(state.get("llm_review_mode", "required"), reviewed, len(kept), reviewed - len(kept))


def _ask_model_for_decisions(client: OpenAICompatibleClient, candidate_type: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把候选交给模型复判，并取回 decisions 数组。"""

    response = client.complete_json(
        system_prompt=_system_prompt(candidate_type),
        user_prompt=json.dumps({"candidates": candidates}, ensure_ascii=False, indent=2),
    )
    decisions = response.get("decisions", [])
    if not isinstance(decisions, list):
        raise ValueError("模型复判 JSON 中 decisions 必须是数组")
    return decisions


def _system_prompt(candidate_type: str) -> str:
    """模型复判系统提示词。"""

    object_name = "数据表" if candidate_type == "table" else "字段/指标"
    return (
        f"你是数仓治理专家，需要判断算法召回的{object_name}候选是否在治理意义上真正相似。"
        "判断标准：业务对象一致、核心口径一致、命名或中文注释同义、字段集合或指标含义可复用。"
        "如果只是同一业务域但粒度、时间窗口、对象、事实过程或指标口径明显不同，应判断为不相似。"
        "只输出 JSON，不要输出解释性正文。JSON 格式："
        '{"decisions":[{"index":0,"is_similar":true,"reason":"简短中文理由"}]}'
    )


def _table_candidates(matches: Sequence[TableMatch], metadata: _ReviewMetadata, state: GraphState) -> List[Dict[str, Any]]:
    """构造表候选 prompt 内容。"""

    source_table = state.get("source_table")
    ddl_columns = source_table.columns if source_table and state["intent"] == "single_table" else []
    candidates: List[Dict[str, Any]] = []
    for index, match in enumerate(matches):
        source = metadata.table_key(match.source_database_name, match.source_table_name)
        target = metadata.table_key(match.target_database_name, match.target_table_name)
        candidates.append(
            {
                "index": index,
                "source_database_name": match.source_database_name if ddl_columns else source[0],
                "source_table_name": match.source_table_name,
                "source_table_comment": match.source_table_comment,
                "target_table_name": match.target_table_name,
                "target_database_name": target[0],
                "target_table_comment": match.target_table_comment,
                "algorithm_name_similarity": round(match.name_similarity, 4),
                "algorithm_field_similarity": None if match.field_similarity is None else round(match.field_similarity, 4),
                "source_columns": _prompt_columns(ddl_columns or metadata.tables.get(source, {}).values()),
                "target_columns": _prompt_columns(metadata.tables.get(target, {}).values()),
                "diagnostics": {
                    direction: {**details, "best_matches": details["best_matches"][:_PROMPT_COLUMN_LIMIT]}
                    for direction, details in match.diagnostics.items()
                },
            }
        )
    return candidates


def _column_candidates(matches: Sequence[ColumnMatch], metadata: _ReviewMetadata, state: GraphState) -> List[Dict[str, Any]]:
    """构造字段候选 prompt 内容。"""

    source_column = state.get("source_column") if state["intent"] == "single_column" else None
    candidates: List[Dict[str, Any]] = []
    for index, match in enumerate(matches):
        source = metadata.table_key(match.source_database_name, match.source_table_name)
        target = metadata.table_key(match.target_database_name, match.target_table_name)
        source_meta = metadata.tables.get(source, {}).get(match.source_column_name)
        target_meta = metadata.tables.get(target, {}).get(match.target_column_name)
        candidates.append(
            {
                "index": index,
                "source_database_name": source[0],
                "source_column_name": match.source_column_name,
                "source_table_name": match.source_table_name,
                "source_column_comment": getattr(source_column, "column_comment", "") or getattr(source_meta, "column_comment", ""),
                "target_column_name": match.target_column_name,
                "target_table_name": match.target_table_name,
                "target_database_name": target[0],
                "target_column_comment": getattr(target_meta, "column_comment", ""),
                "target_table_comment": getattr(target_meta, "table_comment", ""),
                "source_window": match.source_window,
                "target_window": match.target_window,
                "window_relation": match.window_relation,
                "algorithm_similarity": round(match.similarity, 4),
            }
        )
    return candidates


def _build_client_from_state(state: GraphState) -> OpenAICompatibleClient:
    """从 LangGraph state 构建模型客户端。"""

    api_base = state.get("llm_api_base", "")
    model = state.get("llm_model", "")
    if not api_base or not model:
        raise ValueError("模型复判已启用，但缺少 llm_api_base 或 llm_model 配置")
    return OpenAICompatibleClient(
        LLMClientConfig(
            api_base=api_base,
            api_key=state.get("llm_api_key", ""),
            model=model,
            timeout_seconds=int(state.get("llm_timeout_seconds", 60)),
        )
    )


def _chat_completions_url(api_base: str) -> str:
    """生成 chat completions 请求 URL。"""

    normalized = api_base.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _responses_url(api_base: str) -> str:
    """生成 responses 请求 URL。"""

    normalized = api_base.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    return f"{normalized}/responses"


def _extract_responses_content(response_body: str) -> str:
    """从 OpenAI Responses API 响应中提取最终文本。"""

    raw_json = json.loads(response_body)
    output_parts: List[str] = []
    for item in raw_json.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                output_parts.append(content["text"])
    text = "".join(output_parts).strip()
    if not text and raw_json.get("output_text"):
        text = str(raw_json["output_text"]).strip()
    if not text:
        raise ValueError(f"responses 响应中未提取到文本：{response_body[:300]}")
    return text


def _decision_map(decisions: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """将模型 decisions 数组转换为 index -> decision 映射。"""

    mapped: Dict[int, Dict[str, Any]] = {}
    for decision in decisions:
        try:
            mapped[int(decision["index"])] = decision
        except (KeyError, TypeError, ValueError):
            continue
    return mapped


def _prompt_columns(columns: Iterable[ColumnInfo | MetadataRow]) -> list[dict[str, str]]:
    return [
        {"column_name": column.column_name, "column_type": column.column_type, "column_comment": column.column_comment}
        for column in islice(columns, _PROMPT_COLUMN_LIMIT)
    ]


def _batches(values: Sequence[Any], batch_size: int) -> Iterable[Sequence[Any]]:
    """按固定大小切分候选，控制单次模型输入长度。"""

    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def _summary(mode: str, reviewed: int, kept: int, filtered: int) -> Dict[str, Any]:
    """生成模型复判摘要。"""

    return {"mode": mode, "reviewed": reviewed, "kept": kept, "filtered": filtered}


def _current_match_count(state: GraphState) -> int:
    """统计当前候选数量。"""

    if state.get("intent") in {"single_table", "all_tables"}:
        return len(state.get("table_matches", []))
    return len(state.get("column_matches", []))
