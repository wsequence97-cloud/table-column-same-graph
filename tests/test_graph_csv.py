from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from table_column_same_graph.graph import build_graph


def test_graph_writes_csv_and_memory(tmp_path):
    metadata_csv = Path(__file__).resolve().parents[1] / "examples" / "sample_metadata.csv"
    graph = build_graph()

    result = graph.invoke(
        {
            "user_input": "表 dwd_trade_order_pay_detail_di",
            "source_type": "csv",
            "metadata_csv": str(metadata_csv),
            "output_dir": str(tmp_path / "outputs"),
            "memory_dir": str(tmp_path / "memory"),
            "llm_review_mode": "disabled",
        }
    )

    assert Path(result["output_file"]).exists()
    assert result["table_matches"]
    assert (tmp_path / "memory" / "similarity_memory.jsonl").exists()
