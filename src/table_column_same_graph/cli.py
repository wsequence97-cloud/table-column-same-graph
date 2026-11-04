"""命令行入口。

示例：python -m table_column_same_graph.cli --query "表 dwd_trade_order_pay_detail_di"
"""

import argparse
import asyncio
from pathlib import Path
from typing import List

from table_column_same_graph.config import RUNTIME_CONFIG
from table_column_same_graph.graph import build_graph


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器。"""

    parser = argparse.ArgumentParser(description="相似数据表/字段识别 LangGraph")
    parser.add_argument("--query", required=True, help="用户输入，例如：表 dwd_trade_order_pay_detail_di / 字段 pay_id / 全表比对")
    parser.add_argument("--output-dir", default=str(RUNTIME_CONFIG.output_dir), help="CSV 输出目录")
    parser.add_argument("--memory-dir", default=str(RUNTIME_CONFIG.memory_dir), help="相似检索记忆目录")
    parser.add_argument(
        "--llm-review-mode",
        choices=["required", "optional", "disabled"],
        default=None,
        help="模型复判模式：required=必须复判后输出，optional=失败时保留算法结果，disabled=跳过复判",
    )
    parser.add_argument("--llm-api-base", default=None, help="OpenAI-compatible API base，例如 https://api.openai.com/v1")
    parser.add_argument("--llm-api-key", default=None, help="模型 API Key；也可用 SAME_GRAPH_LLM_API_KEY 环境变量")
    parser.add_argument("--llm-model", default=None, help="模型名称；也可用 SAME_GRAPH_LLM_MODEL 环境变量")
    parser.add_argument("--llm-timeout-seconds", type=int, default=None, help="模型请求超时时间")
    parser.add_argument("--llm-batch-size", type=int, default=None, help="每批交给模型复判的候选数量")
    return parser


def main(argv: List[str] = None) -> int:
    """CLI 主函数。"""

    args = build_parser().parse_args(argv)
    graph = build_graph()
    result = asyncio.run(graph.ainvoke(
        {
            "user_input": args.query,
            "output_dir": str(Path(args.output_dir).expanduser()),
            "memory_dir": str(Path(args.memory_dir).expanduser()),
            "llm_review_mode": args.llm_review_mode,
            "llm_api_base": args.llm_api_base,
            "llm_api_key": args.llm_api_key,
            "llm_model": args.llm_model,
            "llm_timeout_seconds": args.llm_timeout_seconds,
            "llm_batch_size": args.llm_batch_size,
        }
    ))
    if result.get("validation_status") == "INVALID":
        print(f"输入校验失败：{result['message']}")
        print(f"建议输入：\n{result['suggestion']}")
        return 2
    print(f"CSV 输出文件：{result.get('output_file', '')}")
    print(f"匹配意图：{result.get('intent', '')}")
    print(f"元数据行数：{len(result.get('metadata_rows', []))}")
    print(f"元数据快照：{result.get('metadata_snapshot_pt', '')}")
    print(f"模型复判：{result.get('llm_review_summary', {})}")
    print(f"记忆已沉淀：{bool(result.get('memory_summary'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
