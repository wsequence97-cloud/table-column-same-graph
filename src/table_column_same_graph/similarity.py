"""规则相似度：表名召回、字段覆盖率与字段候选诊断。"""

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, Iterable, List, Sequence

from table_column_same_graph.config import SIMILARITY_CONFIG
from table_column_same_graph.models import ColumnInfo, ColumnMatch, MetadataRow, TableInfo, TableMatch
from table_column_same_graph.text_utils import (
    NameFeatures, bidirectional_feature_similarity, compare_windows, extract_window,
    feature_similarity, mixed_name_similarity, name_features, safe_ratio,
)


def dedupe_tables(rows: Sequence[MetadataRow]) -> List[TableInfo]:
    """将字段按物理表 (database_name, table_name) 归组。"""
    tables = {}
    for row in rows:
        key = (row.database_name, row.table_name)
        if key not in tables:
            tables[key] = TableInfo(table_name=row.table_name, table_comment=row.table_comment,
                                    database_name=row.database_name)
        tables[key].columns.append(ColumnInfo(
            table_name=row.table_name, table_comment=row.table_comment, database_name=row.database_name,
            column_name=row.column_name, column_type=row.column_type, column_comment=row.column_comment,
        ))
    return list(tables.values())


@dataclass(slots=True)
class _Field:
    column: ColumnInfo | MetadataRow
    tokens: NameFeatures | None
    window: str | None

    @classmethod
    def prepare(cls, column, use_cache=True):
        tokens = name_features(column.column_name, column.column_comment, True) if use_cache else None
        return cls(column, tokens, extract_window(column.column_name))

    def features(self):
        if self.tokens is not None:
            return self.tokens
        return name_features(self.column.column_name, self.column.column_comment, True)


@dataclass(slots=True)
class _Table:
    table: TableInfo
    tokens: NameFeatures | None
    fields: list[_Field]

    @classmethod
    def prepare(cls, table, use_cache=True, include_fields=True):
        tokens = name_features(table.table_name, table.table_comment) if use_cache else None
        # 同分时选择字典序最小的目标字段，使映射独立于分页和字段排列。
        fields = []
        if include_fields:
            for col in sorted(table.columns, key=lambda col: col.column_name):
                fields.append(_Field.prepare(col, use_cache))
        return cls(table, tokens, fields)

    def features(self):
        if self.tokens is not None:
            return self.tokens
        return name_features(self.table.table_name, self.table.table_comment)


def _coverage_report(source, target, best_matches):
    mappings = []
    used_targets = set()
    for field, (score, target_index) in zip(source, best_matches):
        if target_index is None or score < SIMILARITY_CONFIG.table_column_threshold:
            continue
        best = target[target_index]
        used_targets.add(target_index)
        mappings.append({
            "source_column": field.column.column_name, "target_column": best.column.column_name,
            "similarity": score, "source_window": field.window, "target_window": best.window,
            "window_relation": compare_windows(field.window, best.window),
        })
    return {
        "field_coverage": safe_ratio(len(mappings), len(source)),
        "source_field_count": len(source), "matched_field_count": len(mappings),
        "many_to_one": len(mappings) > len(used_targets), "best_matches": mappings,
    }


def _field_coverages(left, right, bidirectional):
    """单次遍历字段对，每个方向独立选最佳匹配，空间不随字段对数量增长。"""
    left_best = [(0.0, None)] * len(left)
    right_best = [(0.0, None)] * len(right) if bidirectional else []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            if bidirectional:
                forward, reverse = bidirectional_feature_similarity(a.features(), b.features())
                if reverse > right_best[j][0]:
                    right_best[j] = (reverse, i)
            else:
                forward = feature_similarity(a.features(), b.features())
            if forward > left_best[i][0]:
                left_best[i] = (forward, j)
    reports = {"source_to_target": _coverage_report(left, right, left_best)}
    if bidirectional:
        reports["target_to_source"] = _coverage_report(right, left, right_best)
    return reports


def _table_match(left, right, name_score, field_score=None, diagnostics=None):
    return TableMatch(
        source_table_name=left.table_name, source_table_comment=left.table_comment,
        source_database_name=left.database_name, target_database_name=right.database_name,
        target_table_name=right.table_name, target_table_comment=right.table_comment,
        name_similarity=name_score, field_similarity=field_score, diagnostics=diagnostics or {},
    )


def _table_rank(match):
    score = match.name_similarity if match.field_similarity is None else match.field_similarity
    return (-score, match.source_database_name, match.source_table_name,
            match.target_database_name, match.target_table_name)


def find_single_table_matches(source_table: TableInfo, rows: Sequence[MetadataRow]) -> List[TableMatch]:
    """保留用户输入到目标的名称及 DDL 字段覆盖率。"""
    tables = dedupe_tables(rows)
    source = _Table.prepare(source_table)
    same_name_count = sum(table.table_name == source_table.table_name for table in tables)
    matches = []
    for table in tables:
        if source_table.table_name == table.table_name and (
            source_table.database_name == table.database_name
            or (not source_table.database_name and same_name_count == 1)
        ):
            continue
        target = _Table.prepare(table, include_fields=bool(source.fields))
        name_score = feature_similarity(source.features(), target.features())
        if name_score < SIMILARITY_CONFIG.table_name_threshold:
            continue
        if not source.fields:
            matches.append(_table_match(source_table, table, name_score))
            continue
        report = _field_coverages(source.fields, target.fields, bidirectional=False)
        forward = report["source_to_target"]
        forward["name_score"] = name_score
        if forward["field_coverage"] >= SIMILARITY_CONFIG.table_column_threshold:
            matches.append(_table_match(source_table, table, name_score, forward["field_coverage"], report))
    return sorted(matches, key=_table_rank)


def find_all_table_matches(rows: Sequence[MetadataRow]) -> List[TableMatch]:
    """全表两阶段分别取两个方向的最大值，每对物理表只输出一次。"""
    return _find_all_table_matches(rows)


def _find_all_table_matches(rows, *, use_cache=True, bidirectional=True):
    """基准实验可关闭预分词或反向计算，生产入口使用默认策略。"""
    ordered = sorted(dedupe_tables(rows), key=lambda t: (t.database_name, t.table_name))
    tables = [_Table.prepare(t, use_cache) for t in ordered]
    matches = []
    for left, right in combinations(tables, 2):
        if bidirectional:
            forward, reverse = bidirectional_feature_similarity(left.features(), right.features())
            name_score = max(forward, reverse)
        else:
            forward = name_score = feature_similarity(left.features(), right.features())
        if name_score < SIMILARITY_CONFIG.table_name_threshold:
            continue
        report = _field_coverages(left.fields, right.fields, bidirectional)
        report["source_to_target"]["name_score"] = forward
        if bidirectional:
            report["target_to_source"]["name_score"] = reverse
        field_score = max(direction["field_coverage"] for direction in report.values())
        if field_score >= SIMILARITY_CONFIG.all_table_field_threshold:
            matches.append(_table_match(left.table, right.table, name_score, field_score, report))
    return sorted(matches, key=_table_rank)


def _column_match(left, right, score):
    a, b = left.column, right.column
    return ColumnMatch(
        source_column_name=a.column_name, source_table_name=a.table_name, source_database_name=a.database_name,
        target_column_name=b.column_name, target_table_name=b.table_name, target_database_name=b.database_name,
        similarity=score, source_window=left.window, target_window=right.window,
        window_relation=compare_windows(left.window, right.window),
    )


def _column_rank(match):
    return (-match.similarity, match.source_database_name, match.source_table_name, match.source_column_name,
            match.target_database_name, match.target_table_name, match.target_column_name)


def find_single_column_matches(source_column: ColumnInfo, rows: Sequence[MetadataRow]) -> List[ColumnMatch]:
    source = _Field.prepare(source_column)
    matches = []
    for row in rows:
        target = _Field.prepare(row)
        score = feature_similarity(source.tokens, target.tokens)
        if score >= SIMILARITY_CONFIG.column_threshold:
            matches.append(_column_match(source, target, score))
    return sorted(matches, key=_column_rank)


def find_all_column_matches(rows: Sequence[MetadataRow]) -> List[ColumnMatch]:
    """沿用字段输入顺序和单向分数，每个物理字段对只比较一次。"""
    fields = [_Field.prepare(row) for row in rows]
    matches = []
    for left, right in combinations(fields, 2):
        a, b = left.column, right.column
        if (a.column_name == b.column_name and a.table_name == b.table_name and a.database_name == b.database_name):
            continue
        score = feature_similarity(left.tokens, right.tokens)
        if score >= SIMILARITY_CONFIG.all_column_threshold:
            matches.append(_column_match(left, right, score))
    return sorted(matches, key=_column_rank)


def table_name_similarity(source_table: TableInfo, target_table: TableInfo) -> float:
    return mixed_name_similarity(source_table.table_name, source_table.table_comment,
                                 target_table.table_name, target_table.table_comment)


def column_similarity(source_column: ColumnInfo, target_column: ColumnInfo) -> float:
    return mixed_name_similarity(source_column.column_name, source_column.column_comment,
                                 target_column.column_name, target_column.column_comment, drop_window_suffix=True)


def table_field_similarity(source_columns: Sequence[ColumnInfo], target_columns: Sequence[ColumnInfo]) -> float:
    left = [_Field.prepare(col) for col in source_columns]
    right = [_Field.prepare(col) for col in target_columns]
    return _field_coverages(left, right, bidirectional=False)["source_to_target"]["field_coverage"]


def best_column_similarity(source_column: ColumnInfo, target_columns: Iterable[ColumnInfo]) -> float:
    source = _Field.prepare(source_column)
    return max((feature_similarity(source.tokens, _Field.prepare(col).tokens) for col in target_columns), default=0.0)


def summarize_table_terms(table: TableInfo) -> Dict[str, List[str]]:
    """提取表核心词，供记忆沉淀使用。

    该函数放在 similarity 模块中是因为它与匹配口径保持一致，避免记忆与实际算法脱节。
    """

    from table_column_same_graph.text_utils import split_chinese_comment, split_english_identifier

    return {
        "name_terms": split_english_identifier(table.table_name),
        "comment_terms": split_chinese_comment(table.table_comment),
    }


def summarize_column_terms(column: ColumnInfo) -> Dict[str, List[str]]:
    """提取字段核心词，供记忆沉淀使用。"""

    from table_column_same_graph.text_utils import split_chinese_comment, split_english_identifier

    return {
        "name_terms": split_english_identifier(column.column_name, drop_window_suffix=True),
        "comment_terms": split_chinese_comment(column.column_comment),
    }
