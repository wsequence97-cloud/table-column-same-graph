"""用户输入解析。

负责把自然输入转为结构化的表/字段对象，并识别要走哪条 LangGraph 分支。
"""

import re
from typing import List, Tuple

from table_column_same_graph.models import ColumnInfo, Intent, TableInfo
from table_column_same_graph.text_utils import normalize_identifier


CREATE_DDL_RE = re.compile(r"\bcreate\s+(?:external\s+)?(?:table|view)\b", re.IGNORECASE)


def detect_intent(user_input: str) -> Tuple[Intent, bool]:
    """识别用户输入意图。

    规则顺序很重要：
    1. 全表/全字段口令优先；
    2. 明确“字段”前缀走字段单次查询；
    3. 明确“表”前缀或 DDL 走表单次查询；
    4. 不规范输入按用户要求默认走数据表相似查询。
    """

    text = (user_input or "").strip()
    lowered = text.lower()
    has_ddl = bool(CREATE_DDL_RE.search(text))

    if "全表比对" in text:
        return "all_tables", has_ddl
    if "全字段比对" in text or "全指标比对" in text:
        return "all_columns", has_ddl
    if text.startswith("字段"):
        return "single_column", has_ddl
    if text.startswith("表") or has_ddl:
        return "single_table", has_ddl
    # column string comment '' 这种字段片段如果没有“字段”前缀，仍可识别为字段。
    if re.search(r"^\s*`?[a-zA-Z_][\w]*`?\s+\w+.*\bcomment\b", lowered, re.IGNORECASE):
        return "single_column", has_ddl
    return "single_table", has_ddl


def strip_intent_prefix(user_input: str) -> str:
    """去掉“表/字段”意图前缀，保留真实待匹配内容。"""

    text = (user_input or "").strip()
    if text.startswith("表"):
        return text[1:].strip(" ：:，,")
    if text.startswith("字段"):
        return text[2:].strip(" ：:，,")
    return text


def parse_table_input(user_input: str) -> TableInfo:
    """解析表输入。

    支持三类输入：
    1. 表 + 英文表名；
    2. 表 + 表中文名；
    3. create table/create view DDL。
    """

    text = strip_intent_prefix(user_input)
    if CREATE_DDL_RE.search(text):
        return parse_table_ddl(text)

    # 非 DDL 输入里如果包含明显英文标识符，认为它是表名；否则作为表中文名。
    normalized = normalize_identifier(text)
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_\.]*", text.strip("`")) and normalized:
        return TableInfo(table_name=normalized)
    return TableInfo(table_comment=text.strip())


def parse_column_input(user_input: str) -> ColumnInfo:
    """解析字段/指标输入。

    支持字段名、字段中文名、以及 column string comment '' 片段。
    """

    text = strip_intent_prefix(user_input)
    parsed = parse_column_definition(text)
    if parsed.column_name or parsed.column_comment:
        return parsed

    normalized = normalize_identifier(text)
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", text.strip("`")) and normalized:
        return ColumnInfo(column_name=normalized)
    return ColumnInfo(column_comment=text.strip())


def parse_table_ddl(ddl: str) -> TableInfo:
    """解析 Hive create table/create view DDL。

    该解析器按治理场景做轻量解析，不追求覆盖所有 SQL 方言；
    重点提取表名、表 comment、字段名、字段类型和字段 comment。
    """

    table_name = ""
    table_comment = ""
    columns: List[ColumnInfo] = []

    # 提取 create table/view 后的对象名，兼容 `zz`.`table`、zz.table、`table`。
    table_match = re.search(
        r"\bcreate\s+(?:external\s+)?(?:table|view)\s+(?:if\s+not\s+exists\s+)?([`.\w]+)",
        ddl,
        re.IGNORECASE,
    )
    if table_match:
        table_name = normalize_identifier(table_match.group(1))

    # 表级 COMMENT 通常出现在字段括号之后；必须从 create table 第一层字段括号结束处之后再找，
    # 避免把 DECIMAL(18,2) COMMENT '字段中文' 误识别为表中文名。
    columns_end = _find_first_parenthesized_block_end(ddl)
    table_tail = ddl[columns_end:] if columns_end >= 0 else ddl
    comment_match = re.search(r"\bcomment\s+['\"]([^'\"]+)['\"]", table_tail, re.IGNORECASE | re.DOTALL)
    if comment_match:
        table_comment = comment_match.group(1).strip()

    # 只截取 create table 后第一层括号内的字段定义，避免分区字段和表属性干扰主字段匹配。
    columns_block = _extract_first_parenthesized_block(ddl)
    if columns_block:
        for raw_column in _split_column_lines(columns_block):
            column = parse_column_definition(raw_column)
            if column.column_name:
                columns.append(column)

    return TableInfo(table_name=table_name, table_comment=table_comment, columns=columns)


def parse_column_definition(raw_column: str) -> ColumnInfo:
    """解析单个字段定义。

    示例：pay_id STRING COMMENT '支付号'
    输出：column_name=pay_id, column_type=STRING, column_comment=支付号
    """

    text = (raw_column or "").strip().rstrip(",")
    # 排除 DDL 中非字段定义的约束或分区片段。
    if not text or text.lower().startswith(("partitioned", "stored", "tblproperties", "comment", "primary")):
        return ColumnInfo()

    match = re.match(r"`?([a-zA-Z_][\w]*)`?\s+([a-zA-Z0-9_<>(),]+)(.*)$", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ColumnInfo()

    column_name = normalize_identifier(match.group(1))
    column_type = match.group(2).strip()
    tail = match.group(3) or ""
    comment = ""
    comment_match = re.search(r"\bcomment\s+['\"]([^'\"]*)['\"]", tail, re.IGNORECASE | re.DOTALL)
    if comment_match:
        comment = comment_match.group(1).strip()
    return ColumnInfo(column_name=column_name, column_type=column_type, column_comment=comment)


def _extract_first_parenthesized_block(sql: str) -> str:
    """提取 SQL 中第一组顶层括号内容。

    Hive 字段类型可能包含 decimal(10,2)，所以不能简单用正则匹配到第一个右括号。
    """

    start = sql.find("(")
    if start < 0:
        return ""

    depth = 0
    in_quote = ""
    for index in range(start, len(sql)):
        char = sql[index]
        if char in {"'", '"'}:
            # 进入或退出字符串，避免 comment 中的括号影响层级判断。
            if in_quote == char:
                in_quote = ""
            elif not in_quote:
                in_quote = char
        if in_quote:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[start + 1 : index]
    return ""


def _find_first_parenthesized_block_end(sql: str) -> int:
    """返回第一组顶层括号结束位置。

    找不到时返回 -1；表级 comment 解析会基于这个位置继续向后查找。
    """

    start = sql.find("(")
    if start < 0:
        return -1

    depth = 0
    in_quote = ""
    for index in range(start, len(sql)):
        char = sql[index]
        if char in {"'", '"'}:
            if in_quote == char:
                in_quote = ""
            elif not in_quote:
                in_quote = char
        if in_quote:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _split_column_lines(columns_block: str) -> List[str]:
    """按顶层逗号拆分字段定义。

    字段类型 decimal(10,2) 或 comment 文本中可能包含逗号，拆分时需要跟踪括号和引号。
    """

    lines: List[str] = []
    current: List[str] = []
    depth = 0
    in_quote = ""
    for char in columns_block:
        if char in {"'", '"'}:
            if in_quote == char:
                in_quote = ""
            elif not in_quote:
                in_quote = char
        if not in_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                lines.append("".join(current).strip())
                current = []
                continue
        current.append(char)
    if current:
        lines.append("".join(current).strip())
    return lines
