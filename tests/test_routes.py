"""Flask routes: input handling, the NDJSON progress stream, samples, and export."""

from __future__ import annotations

import io
import json

import pytest

import app as app_module
import fixtures
from analyzer import AnalyzerError

TRANSCRIPT = "Testco Freight earnings call transcript with plenty of detail. " * 20


def read_events(response) -> list[dict]:
    assert response.mimetype == "application/x-ndjson"
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line]


class TestIndex:
    def test_renders(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"Rival <span>Edge</span>" in response.data
        assert b"/static/app.js" in response.data

    def test_warns_when_no_api_key(self, client):
        assert b"Live analysis is off" in client.get("/").data

    def test_no_warning_with_api_key(self, client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert b"Live analysis is off" not in client.get("/").data


class TestAnalyze:
    def test_empty_submission(self, client):
        response = client.post("/api/analyze", data={"text": "  "})
        assert response.status_code == 400
        assert "No document" in response.get_json()["error"]

    def test_unsupported_file_type(self, client):
        response = client.post("/api/analyze", data={"file": (io.BytesIO(b"x"), "model.xlsx")})
        assert response.status_code == 400
        assert ".xlsx" in response.get_json()["error"]

    def test_html_upload_is_converted_to_text(self, client, monkeypatch):
        received = {}

        def fake_analyze(text, progress):
            received["text"] = text
            return fixtures.analysis()

        monkeypatch.setattr(app_module, "analyze_document", fake_analyze)
        html = b"<html><body><p>Revenue was $1.39 billion.</p>" + b"<p>Filler sentence.</p>" * 20 + b"</body></html>"
        client.post("/api/analyze", data={"file": (io.BytesIO(html), "msft-10k.htm")})
        assert "Revenue was $1.39 billion." in received["text"]
        assert "<p>" not in received["text"]

    def test_streams_progress_then_result(self, client, monkeypatch):
        def fake_analyze(text, progress):
            progress("Reading")
            progress("Analyzing")
            return fixtures.analysis()

        monkeypatch.setattr(app_module, "analyze_document", fake_analyze)
        events = read_events(client.post("/api/analyze", data={"text": TRANSCRIPT}))

        assert [e["type"] for e in events] == ["progress", "progress", "result"]
        assert events[-1]["data"]["company_name"] == fixtures.ANALYSIS["company_name"]

    def test_uploaded_file_takes_precedence_over_pasted_text(self, client, monkeypatch):
        received = {}

        def fake_analyze(text, progress):
            received["text"] = text
            return fixtures.analysis()

        monkeypatch.setattr(app_module, "analyze_document", fake_analyze)
        client.post("/api/analyze", data={
            "text": "pasted text",
            "file": (io.BytesIO(TRANSCRIPT.encode()), "call.txt"),
        })
        assert received["text"] == TRANSCRIPT.strip()

    def test_analyzer_errors_arrive_as_error_events(self, client, monkeypatch):
        def failing(text, progress):
            raise AnalyzerError("Rate limited by the Anthropic API.")

        monkeypatch.setattr(app_module, "analyze_document", failing)
        events = read_events(client.post("/api/analyze", data={"text": TRANSCRIPT}))
        assert events == [{"type": "error", "error": "Rate limited by the Anthropic API."}]

    def test_unexpected_errors_are_not_leaked(self, client, monkeypatch):
        def crashing(text, progress):
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr(app_module, "analyze_document", crashing)
        events = read_events(client.post("/api/analyze", data={"text": TRANSCRIPT}))
        assert events[-1]["type"] == "error"
        assert "secret internal detail" not in events[-1]["error"]


class TestCompare:
    def test_requires_both_periods(self, client):
        response = client.post("/api/compare", data={"text_a": TRANSCRIPT})
        assert response.status_code == 400

    def test_streams_comparison(self, client, monkeypatch):
        def fake_compare(earlier, later, progress):
            progress("Comparing")
            return fixtures.comparison_payload()

        monkeypatch.setattr(app_module, "compare_documents", fake_compare)
        events = read_events(client.post("/api/compare", data={"text_a": TRANSCRIPT, "text_b": TRANSCRIPT}))
        assert events[-1]["type"] == "result"
        assert events[-1]["data"]["comparison"]["trajectory"] == "Deteriorating"


class TestSamplesAndExamples:
    @pytest.mark.parametrize("name", ["q2", "q3"])
    def test_bundled_samples_are_served(self, client, name):
        response = client.get(f"/api/samples/{name}")
        assert response.status_code == 200
        assert "Northwind Logistics" in response.get_data(as_text=True)

    def test_unknown_sample_is_404(self, client):
        assert client.get("/api/samples/../app.py").status_code == 404
        assert client.get("/api/samples/q9").status_code == 404

    def test_examples_404_when_not_generated(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "SAMPLES_DIR", tmp_path)
        assert client.get("/api/examples/analysis").status_code == 404

    def test_examples_served_when_present(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "SAMPLES_DIR", tmp_path)
        (tmp_path / "example-analysis.json").write_text(json.dumps(fixtures.analysis()))
        response = client.get("/api/examples/analysis")
        assert response.status_code == 200
        assert response.get_json()["sentiment"] == "Bullish"


class TestExport:
    def test_analysis_pdf(self, client):
        response = client.post("/api/export", json={"mode": "single", "data": fixtures.analysis()})
        assert response.status_code == 200
        assert response.data[:4] == b"%PDF"
        assert "rival-edge-testco-freight-inc.pdf" in response.headers["Content-Disposition"]

    def test_comparison_pdf(self, client):
        response = client.post("/api/export", json={"mode": "compare", "data": fixtures.comparison_payload()})
        assert response.status_code == 200
        assert response.data[:4] == b"%PDF"

    def test_nothing_to_export(self, client):
        assert client.post("/api/export", json={}).status_code == 400


class TestMemoRoute:
    def test_requires_a_prior_result(self, client):
        response = client.post("/api/memo", data={"text": TRANSCRIPT})
        assert response.status_code == 400
        assert "Run an analysis first" in response.get_json()["error"]

    def test_streams_a_memo(self, client, monkeypatch):
        received = {}

        def fake_memo(result, documents, progress):
            received.update(result=result, documents=documents)
            progress("Drafting the memo")
            return fixtures.memo()

        monkeypatch.setattr(app_module, "draft_memo", fake_memo)
        events = read_events(client.post("/api/memo", data={
            "result": json.dumps(fixtures.analysis()),
            "text": TRANSCRIPT,
        }))

        assert [e["type"] for e in events] == ["progress", "result"]
        assert events[-1]["data"]["disclosure_read"] == "Bearish"
        assert received["result"]["company_name"] == fixtures.ANALYSIS["company_name"]
        assert received["documents"] == [TRANSCRIPT]

    def test_both_periods_reach_the_memo(self, client, monkeypatch):
        received = {}
        monkeypatch.setattr(app_module, "draft_memo",
                            lambda result, documents, progress: received.update(docs=documents) or fixtures.memo())
        client.post("/api/memo", data={
            "result": json.dumps(fixtures.comparison_payload()),
            "text": "earlier period text",
            "text_b": "later period text",
        })
        assert received["docs"] == ["earlier period text", "later period text"]

    def test_memo_pdf_export(self, client):
        response = client.post("/api/export", json={"mode": "memo", "data": fixtures.memo()})
        assert response.status_code == 200
        assert response.data[:4] == b"%PDF"
        assert "rival-edge-memo-testco-freight-inc.pdf" in response.headers["Content-Disposition"]
