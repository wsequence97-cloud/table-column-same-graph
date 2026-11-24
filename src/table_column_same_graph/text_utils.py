"""文本标准化和分词工具。

本项目要同时处理中文表注释、英文字段名、Hive 分区后缀等信息。
这里不依赖外部分词库，采用轻量规则拆分，保证在离线环境也能运行。
"""

import re
from typing import Iterable, List, Set

from table_column_same_graph.models import WindowRelation


NameFeatures = tuple[frozenset[str], frozenset[str]]


# 常见数仓层级、表类型和无业务含义的词，用于从中文 comment 中提取核心业务词。
CHINESE_STOP_WORDS: Set[str] = {
    "表",
    "全量",
    "增量",
    "快照",
    "拉链",
    "明细",
    "汇总",
    "统计",
    "数据",
    "元数据",
    "字段",
    "指标",
    "宽表",
    "事实",
    "维度",
    "临时",
    "中间",
    "每日",
    "每天",
    "历史",
}


# 英文名里的技术层级、分区周期、表类型等词不参与业务相似度。
ENGLISH_STOP_WORDS: Set[str] = {
    "ods",
    "dwd",
    "dws",
    "ads",
    "dim",
    "tmp",
    "temp",
    "di",
    "df",
    "dd",
    "mi",
    "mf",
    "detail",
    "summary",
    "sum",
    "fact",
    "wide",
    "table",
    "tbl",
    "id",
}


# 字段名尾部周期后缀，需求中特别要求 1d/14d/30d/60d/90d 等信息去尾。
WINDOW_SUFFIX_RE = re.compile(r"_([0-9]+)(?:days?|d)$", re.IGNORECASE)


# 同义词按完整词元归一，保留编号（num）与数量（cnt）的区别。
TOKEN_SYNONYMS = {
    "payment": "pay",
    "paid": "pay",
    "paying": "pay",
    "付款": "支付",
    "amount": "amt",
    "count": "cnt",
    "number": "num",
    "ratio": "rate",
}


def normalize_identifier(value: str) -> str:
    """标准化英文表名/字段名。

    只保留小写字母、数字和下划线，反引号、库名前缀等 Hive DDL 常见噪音会被清理。
    """

    value = (value or "").strip().strip("`").lower()
    # Hive 表名可能写成 `zz`.`table` 或 zz.table，这里只保留最后的真实对象名。
    if "." in value:
        value = value.split(".")[-1].strip("`")
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_")


def strip_window_suffix(value: str) -> str:
    """去掉字段名尾部窗口周期后缀。

    例如 pay_amt_30d 和 pay_amt_90d 在指标治理中通常是同一业务字段的不同窗口，
    单字段匹配时先去尾可以提升召回。
    """

    normalized = normalize_identifier(value)
    while WINDOW_SUFFIX_RE.search(normalized):
        normalized = WINDOW_SUFFIX_RE.sub("", normalized)
    return normalized


def extract_window(raw_name: str) -> str | None:
    """只识别末尾明确的正整数天数。复合窗口和自然周等口径留待复判。"""
    identifier = (raw_name or "").strip().rsplit(".", 1)[-1].strip("`")
    match = WINDOW_SUFFIX_RE.search(identifier)
    if not match or WINDOW_SUFFIX_RE.search(identifier[:match.start()]):
        return None
    days = match.group(1).lstrip("0")
    return days + "d" if days else None


def compare_windows(left_window: str | None, right_window: str | None) -> WindowRelation:
    """比较已提取的窗口，双方都缺失时也属于未知。"""
    if left_window is None or right_window is None:
        return "unknown"
    return "same" if left_window == right_window else "different"


def window_relation(raw_left: str, raw_right: str) -> WindowRelation:
    return compare_windows(extract_window(raw_left), extract_window(raw_right))


def split_english_identifier(value: str, drop_window_suffix: bool = False) -> List[str]:
    """将英文表名/字段名拆成业务词列表。

    表名会去掉 dwd/di/df/detail 等层级或类型词；字段名可按需求额外去掉窗口后缀。
    """

    normalized = strip_window_suffix(value) if drop_window_suffix else normalize_identifier(value)
    tokens = [token for token in normalized.split("_") if token]
    return [_normalize_token(token) for token in tokens if token not in ENGLISH_STOP_WORDS]


def split_chinese_comment(value: str) -> List[str]:
    """从中文 comment 中提取核心词。

    规则说明：
    1. 先按数仓命名常见分隔符拆段，例如 dwd-交易域-订单域-订单支付明细-增量表；
    2. 再剔除“增量表、明细、数据”等弱业务词；
    3. 对连续中文片段保留 1-4 字 ngram，让“支付号”和“支付”能匹配。
    """

    raw_text = value or ""
    # 删除英文和数字噪音，只保留中文段落；分隔符用于切开“交易域-订单域”。
    chinese_parts = re.findall(r"[\u4e00-\u9fff]+", raw_text)
    tokens: List[str] = []
    for part in chinese_parts:
        # 先按完整片段收集，便于“交易域”“订单域”这类域名直接命中。
        if part and part not in CHINESE_STOP_WORDS:
            tokens.append(_normalize_token(part))
        # 再加入较短 ngram，解决中文没有自然空格导致的包含关系匹配问题。
        max_size = min(4, len(part))
        for size in range(1, max_size + 1):
            for index in range(0, len(part) - size + 1):
                gram = part[index : index + size]
                if gram not in CHINESE_STOP_WORDS and len(gram) > 1:
                    tokens.append(_normalize_token(gram))
    # 保序去重，方便调试和记忆沉淀时阅读。
    return unique_preserve_order(tokens)


def unique_preserve_order(values: Iterable[str]) -> List[str]:
    """按出现顺序去重。"""

    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_token(token: str) -> str:
    """将同义业务词归一为同一个匹配词。"""

    return TOKEN_SYNONYMS.get(token, token)


def token_overlap_score(source_tokens: Iterable[str], target_tokens: Iterable[str]) -> float:
    """计算“源词被目标词覆盖”的比例。

    用户需求强调“当前输入词汇数匹配大于阈值”进入下一阶段，所以分母固定为源词数。
    这样输入 dwd_trade_order_pay_detail_di 时，只要候选表覆盖 trade/order/pay 中大部分词，
    就可以被召回。
    """

    source_set = {token for token in source_tokens if token}
    target_set = {token for token in target_tokens if token}
    if not source_set:
        return 0.0
    return len(source_set & target_set) / len(source_set)


def name_features(name: str, comment: str, drop_window_suffix: bool = False) -> NameFeatures:
    """提取可在同一次匹配中复用的英文名称词集和中文注释词集。"""
    return (
        frozenset(split_english_identifier(name, drop_window_suffix)),
        frozenset(split_chinese_comment(comment)),
    )


def feature_similarity(source: NameFeatures, target: NameFeatures) -> float:
    english, chinese = source
    return max(
        len(english & target[0]) / len(english) if english else 0.0,
        len(chinese & target[1]) / len(chinese) if chinese else 0.0,
    )


def bidirectional_feature_similarity(left: NameFeatures, right: NameFeatures) -> tuple[float, float]:
    """每个通道只求一次交集，再分别使用左右词数计算有方向的覆盖率。"""
    left_en, left_zh = left
    right_en, right_zh = right
    en_count = len(left_en & right_en)
    zh_count = len(left_zh & right_zh)
    return (
        max(safe_ratio(en_count, len(left_en)), safe_ratio(zh_count, len(left_zh))),
        max(safe_ratio(en_count, len(right_en)), safe_ratio(zh_count, len(right_zh))),
    )


def mixed_name_similarity(
    source_name: str,
    source_comment: str,
    target_name: str,
    target_comment: str,
    drop_window_suffix: bool = False,
) -> float:
    """综合英文名和中文 comment 的最大相似度。

    表/字段治理里中文名和英文名经常只有一个更准确，取两者最大值能兼顾召回；
    阈值仍由调用方控制，表默认 60%，字段默认 75%。
    """

    return feature_similarity(
        name_features(source_name, source_comment, drop_window_suffix),
        name_features(target_name, target_comment, drop_window_suffix),
    )


def safe_ratio(numerator: int, denominator: int) -> float:
    """安全比例计算，避免空字段集合导致除零。"""

    if denominator <= 0:
        return 0.0
    return numerator / denominator
