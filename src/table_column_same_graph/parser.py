"""用户输入解析。

负责把自然输入转为结构化的表/字段对象，并识别要走哪条 LangGraph 分支。
"""

import re
from typing import Any, Dict, List, Tuple

from table_column_same_graph.models import ColumnInfo, Intent, TableInfo
from table_column_same_graph.text_utils import normalize_identifier


CREATE_DDL_RE = re.compile(r"\bcreate\s+(?:external\s+)?(?:table|view)\b", re.IGNORECASE)

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
COLUMN_IDENT = rf"(?:{IDENT}|`{IDENT}`)"
TABLE_IDENT = rf"{COLUMN_IDENT}(?:\.{COLUMN_IDENT})?"
COLUMN_DEF = re.compile(rf"{COLUMN_IDENT}\s+[A-Za-z][A-Za-z0-9_]*(?:\s*\([^()]*\)|\s*<[^<>]+>)?(?:\s+COMMENT\s+('[^']*'|\"[^\"]*\"))?\s*", re.I)
DDL_HEAD = re.compile(rf"^CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({TABLE_IDENT})\s*\(", re.I)
DDL_TAIL = re.compile(r"""\s*(?:COMMENT\s+(?:'[^']*'|"[^"]*"))?\s*(?:PARTITIONED\s+BY\s*\([^()]*\))?\s*(?:STORED\s+AS\s+\w+)?\s*(?:TBLPROPERTIES\s*\([^()]*\))?\s*;?\s*""", re.I | re.S)
EXAMPLES = "表 dwd_trade_order_pay_detail_di\n字段 pay_amt_30d\n全表比对\n全字段比对"
MESSAGES = {'FORMAT_ERROR': '输入格式不符合要求', 'MISSING_TARGET': '缺少查询对象', 'INVALID_DDL': 'DDL 不完整或格式无效', 'UNSUPPORTED': '不支持的输入格式'}


def _invalid(code: str, suggestion: str = EXAMPLES, detected_type: str = '', target: str = '') -> Dict[str, Any]:
    return {'validation_status': 'INVALID', 'error_code': code, 'message': MESSAGES[code],
            'suggestion': suggestion, 'detected_type': detected_type, 'extracted_target': target}


def _hint_invalid(text: str) -> Dict[str, Any]:
    """只生成提示，不将提取结果用作查询。"""
    table = re.search(r'\b(?:ods|dwd|dws|ads|dim)_[A-Za-z0-9_]+\b', text, re.I)
    if table:
        target = table.group()
        return _invalid('FORMAT_ERROR', f'表 {target}', 'table', target)
    field = re.search(r'\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b', text)
    if field and ('字段' in text or '指标' in text):
        target = field.group()
        return _invalid('FORMAT_ERROR', f'字段 {target}', 'column', target)
    return _invalid('UNSUPPORTED')


def _valid_ddl(text: str) -> bool:
    head = DDL_HEAD.match(text)
    if not head:
        return False
    end = _find_first_parenthesized_block_end(text)
    if end < 0:
        return False
    block = text[head.end():end - 1]
    if block.rstrip().endswith(','):
        return False
    parts = _split_column_lines(block)
    if not parts or any(not COLUMN_DEF.fullmatch(part) for part in parts):
        return False
    tail = text[end:].strip()
    if not DDL_TAIL.fullmatch(tail):
        return False
    table = parse_table_ddl(text)
    return bool(table.table_name and len(table.columns) == len(parts))


def validate_input(user_input: str) -> Dict[str, Any]:
    """白名单校验；不接受自然语言自动修正后执行。"""
    text = (user_input or '').strip()
    commands = {'全表比对': 'all_tables', '全字段比对': 'all_columns', '全指标比对': 'all_columns'}
    if text in commands:
        return {'validation_status': 'VALID', 'intent': commands[text], 'has_ddl': False}
    if any(text.startswith(command) for command in commands):
        return _invalid('UNSUPPORTED')
    prefix = re.match(r'^(字段|表)(?:\s*[:：]\s*|\s+)(.*)$', text, re.S)
    if text in ('表', '字段'):
        return _invalid('MISSING_TARGET')
    kind, target = (prefix.group(1), prefix.group(2).strip()) if prefix else ('', text)
    if prefix and not target:
        return _invalid('MISSING_TARGET')
    if re.match(r'^CREATE\b', target, re.I):
        if kind == '字段' or not _valid_ddl(target):
            return _invalid('INVALID_DDL')
        return {'validation_status': 'VALID', 'intent': 'single_table', 'has_ddl': True, 'source_table': parse_table_ddl(target)}
    if not prefix:
        return _hint_invalid(text)
    if kind == '表' and re.fullmatch(TABLE_IDENT, target):
        return {'validation_status': 'VALID', 'intent': 'single_table', 'has_ddl': False, 'source_table': TableInfo(table_name=normalize_identifier(target))}
    if kind == '字段' and (re.fullmatch(COLUMN_IDENT, target) or COLUMN_DEF.fullmatch(target)):
        return {'validation_status': 'VALID', 'intent': 'single_column', 'has_ddl': False, 'source_column': parse_column_input(text)}
    if re.fullmatch(r'[\u4e00-\u9fff][\u4e00-\u9fff0-9A-Za-z_-]*', target) and not any(word in target for word in ('是什么意思', '怎么', '如何', '查询一下', '帮我')):
        if kind == '表':
            return {'validation_status': 'VALID', 'intent': 'single_table', 'has_ddl': False, 'source_table': TableInfo(table_comment=target)}
        return {'validation_status': 'VALID', 'intent': 'single_column', 'has_ddl': False, 'source_column': ColumnInfo(column_comment=target)}
    return _invalid('FORMAT_ERROR')


def detect_intent(user_input: str) -> Tuple[Intent, bool]:
    """识别用户输入意图。

    通过白名单后返回意图，非法输入抛出 ValueError。
    """

    result = validate_input(user_input)
    if result['validation_status'] != 'VALID':
        raise ValueError(result['message'])
    return result['intent'], result['has_ddl']


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

    match = re.match(r"`?([a-zA-Z_][\w]*)`?\s+([a-zA-Z][a-zA-Z0-9_]*(?:\s*\([^()]*\)|\s*<[^<>]+>)?)(.*)$", text, re.IGNORECASE | re.DOTALL)
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
