"""相似度计算逻辑。

本模块只负责业务判断，不关心数据来自 Hive 还是 CSV，也不负责写文件。
这样 LangGraph 节点可以把它当作纯函数使用，便于单元测试。
"""

from collections import defaultdict
from itertools import combinations
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from table_column_same_graph.config import SIMILARITY_CONFIG
from table_column_same_graph.models import ColumnInfo, ColumnMatch, MetadataRow, TableInfo, TableMatch
from table_column_same_graph.text_utils import mixed_name_similarity, safe_ratio


def dedupe_tables(rows: Sequence[MetadataRow]) -> List[TableInfo]:
    """从字段粒度元数据中去重得到表粒度信息。

    zz.dwd_meta_table_columns_detail_df 每一行是一个字段，因此表名匹配前必须先按库名+表名去重；
    同时把同表字段收集起来，后续字段集合相似可以复用。
    """

    tables: Dict[Tuple[str, str], TableInfo] = {}
    for row in rows:
        key = (row.database_name, row.table_name)
        if key not in tables:
            tables[key] = TableInfo(
                database_name=row.database_name,
                table_name=row.table_name,
                table_comment=row.table_comment,
                columns=[],
            )
        tables[key].columns.append(
            ColumnInfo(
                table_name=row.table_name,
                table_comment=row.table_comment,
                column_name=row.column_name,
                column_type=row.column_type,
                column_comment=row.column_comment,
            )
        )
    return list(tables.values())


def find_single_table_matches(source_table: TableInfo, rows: Sequence[MetadataRow]) -> List[TableMatch]:
    """查找单个输入表的相似表。

    处理两种场景：
    1. 只有表名/表中文名：输出表名或中文名相似度大于 60% 的表；
    2. 输入完整 DDL：先按表名/中文名召回，再用字段匹配比例过滤，字段一致比例需大于 60%。
    """

    tables = dedupe_tables(rows)
    has_source_columns = bool(source_table.columns)
    matches: List[TableMatch] = []

    for target_table in tables:
        # 如果源表名等于目标表名，说明是同一张表，不作为治理相似候选输出。
        if source_table.table_name and source_table.table_name == target_table.table_name:
            continue

        name_similarity = table_name_similarity(source_table, target_table)
        if name_similarity < SIMILARITY_CONFIG.table_name_threshold:
            continue

        if not has_source_columns:
            matches.append(
                TableMatch(
                    source_table_name=source_table.table_name,
                    source_table_comment=source_table.table_comment,
                    target_table_name=target_table.table_name,
                    target_table_comment=target_table.table_comment,
                    name_similarity=name_similarity,
                )
            )
            continue

        field_similarity = table_field_similarity(source_table.columns, target_table.columns)
        if field_similarity >= SIMILARITY_CONFIG.table_column_threshold:
            matches.append(
                TableMatch(
                    source_table_name=source_table.table_name,
                    source_table_comment=source_table.table_comment,
                    target_table_name=target_table.table_name,
                    target_table_comment=target_table.table_comment,
                    name_similarity=name_similarity,
                    field_similarity=field_similarity,
                )
            )

    return sorted(matches, key=lambda item: (item.field_similarity or item.name_similarity), reverse=True)


def find_all_table_matches(rows: Sequence[MetadataRow]) -> List[TableMatch]:
    """全表比对。

    combinations(tables, 2) 天然只产生 A-B，不产生 B-A，满足用户“表名 a 比对完表名 b 后去重”的要求。
    """

    tables = dedupe_tables(rows)
    matches: List[TableMatch] = []

    for left_table, right_table in combinations(tables, 2):
        name_similarity = table_name_similarity(left_table, right_table)
        # 全表比对仍沿用“表名及 comment 相似进入字段阶段”的规则，避免全仓字段笛卡尔过大。
        if name_similarity < SIMILARITY_CONFIG.table_name_threshold:
            continue

        field_similarity = table_field_similarity(left_table.columns, right_table.columns)
        if field_similarity >= SIMILARITY_CONFIG.all_table_field_threshold:
            matches.append(
                TableMatch(
                    source_table_name=left_table.table_name,
                    source_table_comment=left_table.table_comment,
                    target_table_name=right_table.table_name,
                    target_table_comment=right_table.table_comment,
                    name_similarity=name_similarity,
                    field_similarity=field_similarity,
                )
            )

    return sorted(matches, key=lambda item: item.field_similarity or 0.0, reverse=True)


def find_single_column_matches(source_column: ColumnInfo, rows: Sequence[MetadataRow]) -> List[ColumnMatch]:
    """查找单个输入字段/指标的相似字段。

    中文 comment 和英文字段名分别计算覆盖率，取最大值；
    阈值为 75%，并且字段名会去掉 1d/30d/90d 等尾部窗口后缀。
    """

    matches: List[ColumnMatch] = []
    for row in rows:
        target_column = ColumnInfo(
            table_name=row.table_name,
            table_comment=row.table_comment,
            column_name=row.column_name,
            column_type=row.column_type,
            column_comment=row.column_comment,
        )
        similarity = column_similarity(source_column, target_column)
        if similarity >= SIMILARITY_CONFIG.column_threshold:
            matches.append(
                ColumnMatch(
                    source_column_name=source_column.column_name,
                    source_table_name=source_column.table_name,
                    target_column_name=row.column_name,
                    target_table_name=row.table_name,
                    similarity=similarity,
                )
            )

    return sorted(matches, key=lambda item: item.similarity, reverse=True)


def find_all_column_matches(rows: Sequence[MetadataRow]) -> List[ColumnMatch]:
    """全字段/全指标比对。

    先按字段名聚合，再使用 combinations 做字段对去重；
    如果同名字段分布在多张表，每个字段实例仍会保留表名，便于治理定位。
    """

    columns = [
        ColumnInfo(
            table_name=row.table_name,
            table_comment=row.table_comment,
            column_name=row.column_name,
            column_type=row.column_type,
            column_comment=row.column_comment,
        )
        for row in rows
    ]
    matches: List[ColumnMatch] = []

    for left_column, right_column in combinations(columns, 2):
        # 同一张表同一个字段不需要与自己匹配；源表不同但字段相似才是治理价值点。
        if left_column.table_name == right_column.table_name and left_column.column_name == right_column.column_name:
            continue
        similarity = column_similarity(left_column, right_column)
        if similarity >= SIMILARITY_CONFIG.all_column_threshold:
            matches.append(
                ColumnMatch(
                    source_column_name=left_column.column_name,
                    source_table_name=left_column.table_name,
                    target_column_name=right_column.column_name,
                    target_table_name=right_column.table_name,
                    similarity=similarity,
                )
            )

    return sorted(matches, key=lambda item: item.similarity, reverse=True)


def table_name_similarity(source_table: TableInfo, target_table: TableInfo) -> float:
    """计算表名/表中文名相似度。"""

    return mixed_name_similarity(
        source_table.table_name,
        source_table.table_comment,
        target_table.table_name,
        target_table.table_comment,
    )


def column_similarity(source_column: ColumnInfo, target_column: ColumnInfo) -> float:
    """计算字段/指标相似度。"""

    return mixed_name_similarity(
        source_column.column_name,
        source_column.column_comment,
        target_column.column_name,
        target_column.column_comment,
        drop_window_suffix=True,
    )


def table_field_similarity(source_columns: Sequence[ColumnInfo], target_columns: Sequence[ColumnInfo]) -> float:
    """计算源表字段被目标表字段覆盖的比例。

    对源表每个字段，只要目标表存在一个字段相似度 >= 60%，就认为该字段被匹配；
    最终相似度 = 已匹配源字段数 / 源字段总数。
    """

    if not source_columns:
        return 0.0

    matched_source_count = 0
    for source_column in source_columns:
        if best_column_similarity(source_column, target_columns) >= SIMILARITY_CONFIG.table_column_threshold:
            matched_source_count += 1
    return safe_ratio(matched_source_count, len(source_columns))


def best_column_similarity(source_column: ColumnInfo, target_columns: Iterable[ColumnInfo]) -> float:
    """返回源字段与目标字段集合中的最高相似度。"""

    best_score = 0.0
    for target_column in target_columns:
        best_score = max(best_score, column_similarity(source_column, target_column))
    return best_score


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
