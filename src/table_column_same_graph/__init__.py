"""相似数据表/字段识别 LangGraph 项目包。

这里不在包初始化时直接导入 LangGraph，避免只测试解析/相似度工具时被运行环境
是否已安装 langgraph 影响；真正构建图时再惰性导入。
"""

__all__ = ["build_graph"]

__version__ = "0.1.0"


def build_graph(*args, **kwargs):
    """惰性构建 LangGraph。

    CLI 和调度系统会调用该函数；如果本机未安装 langgraph，会在这里给出明确错误。
    """

    from table_column_same_graph.graph import build_graph as _build_graph

    return _build_graph(*args, **kwargs)
