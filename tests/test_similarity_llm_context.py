import json
from unittest.mock import Mock

import pytest

from table_column_same_graph import llm_review, similarity
from table_column_same_graph.models import ColumnInfo, ColumnMatch, MetadataRow, TableMatch


ROWS = [
    MetadataRow("source", "alarms", "events", "alarm_cnt_7d", "INT", "rolling week"),
    MetadataRow("target", "alarms", "aggregates", "alarm_cnt_7d", "INT", "weekly total"),
    MetadataRow("target", "alarms", "aggregates", "alarm_cnt_30d", "INT", "rolling month"),
    MetadataRow("source", "devices", "devices", "device_id", "INT", "device identifier"),
]


def review(monkeypatch, intent, match, keep=True, **extra):
    key = "table_matches" if intent == "all_tables" else "column_matches"
    complete = Mock(return_value={"decisions": [{"index": 0, "is_similar": keep, "reason": "reviewed"}]})
    monkeypatch.setattr(llm_review.OpenAICompatibleClient, "complete_json", complete)
    result = llm_review.review_matches_with_llm({
        "intent": intent, key: [match], "metadata_rows": ROWS,
        "llm_api_base": "https://example.invalid", "llm_model": "mock", **extra,
    })
    assert result[key] == ([match] if keep else [])
    assert match.llm_reason == "reviewed"
    assert match.llm_is_similar is keep
    complete.assert_called_once()
    return json.loads(complete.call_args.kwargs["user_prompt"])["candidates"][0]


def test_table_review_distinguishes_databases_and_keeps_directional_risk(monkeypatch):
    match, = similarity.find_all_table_matches(ROWS)
    candidate = review(monkeypatch, "all_tables", match)
    assert candidate["source_database_name"] == "source"
    assert candidate["target_database_name"] == "target"
    assert [c["column_comment"] for c in candidate["source_columns"]] == ["rolling week"]
    assert [c["column_comment"] for c in candidate["target_columns"]] == ["weekly total", "rolling month"]
    assert candidate["diagnostics"] == match.diagnostics
    assert candidate["diagnostics"]["source_to_target"]["many_to_one"] is False
    reverse = candidate["diagnostics"]["target_to_source"]
    assert reverse["many_to_one"] is True
    assert any(pair["window_relation"] == "different" for pair in reverse["best_matches"])


def test_column_review_keeps_physical_comments_and_different_windows(monkeypatch):
    match, = [
        candidate for candidate in similarity.find_all_column_matches(ROWS)
        if candidate.source_database_name == "source" and candidate.target_column_name == "alarm_cnt_30d"
    ]
    candidate = review(monkeypatch, "all_columns", match, keep=False,
                       source_column=ColumnInfo(column_comment="全字段比对"))
    assert (candidate["source_database_name"], candidate["target_database_name"]) == ("source", "target")
    assert (candidate["source_column_comment"], candidate["target_column_comment"]) == ("rolling week", "rolling month")
    assert (candidate["source_window"], candidate["target_window"], candidate["window_relation"]) == ("7d", "30d", "different")
    assert candidate["source_column_name"] == "alarm_cnt_7d"
    assert candidate["target_column_name"] == "alarm_cnt_30d"


@pytest.mark.parametrize("intent", ["all_tables", "all_columns"])
def test_legacy_unqualified_matches_use_only_unambiguous_metadata(monkeypatch, intent):
    if intent == "all_tables":
        candidate = review(monkeypatch, intent, TableMatch("alarms", "", "devices", ""))
        assert candidate["source_columns"] == []
        assert [c["column_name"] for c in candidate["target_columns"]] == ["device_id"]
    else:
        candidate = review(monkeypatch, intent, ColumnMatch("alarm_cnt_7d", "alarms", "device_id", "devices", 1))
        assert candidate["source_column_comment"] == ""
        assert candidate["target_column_comment"] == "device identifier"
