"""
The Claude request path end to end, against a local Messages API stub.

Verifies what the app actually sends (model, streaming, schema, fallbacks),
how responses are assembled and parsed, and how failures surface.
"""

from __future__ import annotations

import pytest

import analyzer
from analyzer import AnalyzerError

TRANSCRIPT = "Testco Freight second quarter earnings call. Revenue grew 8.4%. " * 20


def test_analysis_request_shape(claude_stub):
    analyzer.analyze_document(TRANSCRIPT)

    [request] = claude_stub.requests
    body = request["body"]
    assert "/v1/messages" in request["path"]
    assert body["model"] == "claude-opus-5"
    assert body["stream"] is True
    assert body["max_tokens"] == analyzer.MAX_TOKENS
    assert "senior equity research analyst" in body["system"]
    assert body["messages"][0]["content"].startswith("<document>")
    assert body["output_config"]["format"] == {
        "type": "json_schema",
        "schema": analyzer.ANALYSIS_SCHEMA,
    }


def test_refusal_fallbacks_are_requested_by_default(claude_stub):
    analyzer.analyze_document(TRANSCRIPT)

    [request] = claude_stub.requests
    assert request["body"]["fallbacks"] == "default"
    assert analyzer.FALLBACK_BETA in request["headers"]["anthropic-beta"]


def test_refusal_fallbacks_can_be_disabled(claude_stub, monkeypatch):
    monkeypatch.setenv("RIVAL_EDGE_FALLBACKS", "0")
    analyzer.analyze_document(TRANSCRIPT)

    [request] = claude_stub.requests
    assert "fallbacks" not in request["body"]
    assert "anthropic-beta" not in request["headers"]


def test_analysis_result_and_usage(claude_stub):
    result = analyzer.analyze_document(TRANSCRIPT)

    assert result["company_name"] == claude_stub.analysis["company_name"]
    assert result["_meta"]["chunked"] is False
    usage = result["_meta"]["usage"]
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 1200
    assert usage["output_tokens"] == 450
    assert usage["models"] == ["claude-opus-5"]


def test_progress_reports_each_stage(claude_stub):
    messages: list[str] = []
    analyzer.analyze_document(TRANSCRIPT, messages.append)
    assert messages[0].startswith("Reading ")
    assert messages[-1] == "Analyzing with Claude Opus 5"


def test_overlong_lists_from_the_model_are_trimmed(claude_stub):
    claude_stub.comparison["deltas"] = claude_stub.comparison["deltas"] * 3  # 9 deltas
    result = analyzer.compare_documents(TRANSCRIPT, TRANSCRIPT)
    assert len(result["comparison"]["deltas"]) == analyzer.LIST_LIMITS["deltas"]


class TestComparison:
    def test_two_analyses_then_one_comparison(self, claude_stub):
        result = analyzer.compare_documents(TRANSCRIPT, TRANSCRIPT.replace("second", "third"))

        assert len(claude_stub.requests) == 3
        final = claude_stub.requests[-1]["body"]
        assert "comparison note" in final["system"]
        content = final["messages"][0]["content"]
        assert content.index("<earlier_period>") < content.index("<later_period>")
        assert "_meta" not in content
        assert final["output_config"]["format"]["schema"] == analyzer.COMPARISON_SCHEMA
        assert set(result) == {"earlier", "later", "comparison", "_meta"}
        assert result["_meta"]["usage"]["calls"] == 3

    def test_comparison_sees_source_documents_not_just_summaries(self, claude_stub):
        # A figure disclosed only in Q&A may be missing from a summary; the
        # comparison call must still be able to see it.
        later = TRANSCRIPT + " Top ten customers were 31% of revenue, flat."
        analyzer.compare_documents(TRANSCRIPT, later)

        content = claude_stub.requests[-1]["body"]["messages"][0]["content"]
        later_block = content[content.index("<later_period>"):]
        assert "Top ten customers were 31% of revenue, flat." in later_block
        assert "<analysis>" in later_block

    def test_periods_are_analyzed_concurrently(self, claude_stub):
        claude_stub.delay = 0.4
        analyzer.compare_documents(TRANSCRIPT, TRANSCRIPT)

        first, second = claude_stub.requests[0], claude_stub.requests[1]
        assert second["started"] < first["finished"], "period analyses ran sequentially"

    def test_progress_is_labelled_by_period(self, claude_stub):
        messages: list[str] = []
        analyzer.compare_documents(TRANSCRIPT, TRANSCRIPT, messages.append)
        assert any(m.startswith("Earlier period: ") for m in messages)
        assert any(m.startswith("Later period: ") for m in messages)
        assert messages[-1] == "Comparing the two periods"


class TestLongDocuments:
    DOCUMENT = "\n\n".join(f"Section {i}: revenue discussion. " + "detail " * 60 for i in range(20))

    def test_sections_are_condensed_before_analysis(self, claude_stub, long_document_limits):
        result = analyzer.analyze_document(self.DOCUMENT)

        *condense, final = claude_stub.requests
        assert len(condense) == result["_meta"]["chunks"] > 1
        assert all("research associate" in r["body"]["system"] for r in condense)
        assert all("output_config" not in r["body"] for r in condense)
        assert "senior equity research analyst" in final["body"]["system"]
        assert result["_meta"]["chunked"] is True

    def test_condensed_notes_keep_document_order(self, claude_stub, long_document_limits):
        claude_stub.delay = 0.05
        analyzer.analyze_document(self.DOCUMENT)

        content = claude_stub.requests[-1]["body"]["messages"][0]["content"]
        markers = [content.index(f"NOTES FROM SECTION {i} OF") for i in range(1, 4)]
        assert markers == sorted(markers)


class TestFailures:
    def test_refusal(self, claude_stub):
        claude_stub.stop_reason = "refusal"
        with pytest.raises(AnalyzerError, match="declined"):
            analyzer.analyze_document(TRANSCRIPT)

    def test_truncated_output(self, claude_stub):
        claude_stub.stop_reason = "max_tokens"
        with pytest.raises(AnalyzerError, match="length limit"):
            analyzer.analyze_document(TRANSCRIPT)

    def test_rejected_api_key(self, claude_stub):
        claude_stub.status = 401
        with pytest.raises(AnalyzerError, match="rejected the API key"):
            analyzer.analyze_document(TRANSCRIPT)
