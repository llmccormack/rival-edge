"""Pure logic in analyzer.py: schemas, chunking, text handling, PDF input."""

from __future__ import annotations

import io

import pytest
from fpdf import FPDF

import analyzer
import documents
from analyzer import AnalyzerError
from documents import DocumentError


def _walk(schema, path="$"):
    yield path, schema
    if isinstance(schema, dict):
        for key, value in schema.get("properties", {}).items():
            yield from _walk(value, f"{path}.{key}")
        if "items" in schema:
            yield from _walk(schema["items"], f"{path}[]")


@pytest.mark.parametrize("schema", [analyzer.ANALYSIS_SCHEMA, analyzer.COMPARISON_SCHEMA])
class TestSchemas:
    def test_arrays_avoid_unsupported_count_constraints(self, schema):
        # Structured outputs reject minItems/maxItems other than 0 or 1 with a 400.
        for path, node in _walk(schema):
            for keyword in ("minItems", "maxItems"):
                assert node.get(keyword, 0) <= 1, f"{path} sets {keyword}={node[keyword]}"

    def test_every_object_is_closed_and_fully_required(self, schema):
        for path, node in _walk(schema):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, path
                assert set(node["required"]) == set(node["properties"]), path

    def test_every_list_field_has_a_limit(self, schema):
        for key, node in schema["properties"].items():
            if node.get("type") == "array":
                assert key in analyzer.LIST_LIMITS, f"{key} has no LIST_LIMITS entry"


def test_enforce_list_limits_trims_overlong_lists():
    result = {"top_risks": list("abcdef"), "deltas": list(range(9)), "headline": "unchanged"}
    trimmed = analyzer.enforce_list_limits(result)
    assert trimmed["top_risks"] == list("abc")
    assert len(trimmed["deltas"]) == 6
    assert trimmed["headline"] == "unchanged"


def test_normalize_text_collapses_whitespace_without_losing_content():
    raw = "Revenue   was\t$1.42B.\r\n\r\n\r\n\r\nGuidance raised."
    assert analyzer.normalize_text(raw) == "Revenue was $1.42B.\n\nGuidance raised."


class TestChunking:
    def test_short_document_is_one_chunk(self):
        assert len(analyzer.split_into_chunks("A paragraph about revenue. " * 20)) == 1

    def test_long_document_splits_within_limits(self, long_document_limits):
        text = "\n\n".join(f"Paragraph {i} " + "x" * 400 for i in range(40))
        chunks = analyzer.split_into_chunks(text)
        assert len(chunks) > 1
        assert all(len(c) <= analyzer.CHUNK_CHARS + analyzer.CHUNK_OVERLAP + 2 for c in chunks)

    def test_every_paragraph_survives(self, long_document_limits):
        text = "\n\n".join(f"Paragraph {i} " + "x" * 400 for i in range(40))
        joined = "\n\n".join(analyzer.split_into_chunks(text))
        assert all(f"Paragraph {i} " in joined for i in range(40))

    def test_oversized_paragraph_is_hard_split_not_dropped(self, long_document_limits):
        giant = "y" * (analyzer.CHUNK_CHARS * 2 + 10)
        chunks = analyzer.split_into_chunks(giant)
        assert len(chunks) == 3
        assert sum(len(c) for c in chunks) == len(giant)


def test_too_short_document_is_rejected_before_any_api_call():
    with pytest.raises(AnalyzerError, match="too short"):
        analyzer.analyze_document("Revenue was up.")


def test_missing_api_key_gives_setup_instructions():
    with pytest.raises(AnalyzerError, match="ANTHROPIC_API_KEY"):
        analyzer.analyze_document("A long enough earnings call transcript. " * 20)


class TestPdfExtraction:
    @staticmethod
    def _pdf(text: str) -> io.BytesIO:
        document = FPDF()
        document.add_page()
        document.set_font("Helvetica", size=11)
        if text:
            document.multi_cell(0, 6, text=text)
        return io.BytesIO(bytes(document.output()))

    def test_extracts_text_and_figures(self):
        text = documents.extract_text_from_pdf(
            self._pdf("Third quarter revenue of $1.39 billion, down from plan. " * 10)
        )
        assert "$1.39 billion" in text

    def test_image_only_pdf_is_explained(self):
        with pytest.raises(DocumentError, match="scanned image"):
            documents.extract_text_from_pdf(self._pdf(""))

    def test_corrupt_pdf_is_explained(self):
        with pytest.raises(DocumentError, match="could not be read"):
            documents.extract_text_from_pdf(io.BytesIO(b"%PDF-1.4 not really a pdf"))


class TestAnalyticalContract:
    """The fields a reader checks first must be structured, not prose."""

    def test_guidance_is_a_bridge_not_a_paragraph(self):
        bridge = analyzer.ANALYSIS_SCHEMA["properties"]["guidance_bridge"]
        columns = bridge["items"]["properties"]
        assert set(columns) == {"metric", "prior", "current", "change"}
        assert "midpoint" in columns["change"]["description"]

    def test_reported_financials_are_a_table(self):
        columns = analyzer.ANALYSIS_SCHEMA["properties"]["financial_summary"]["items"]["properties"]
        assert set(columns) == {"metric", "current", "prior", "change"}

    def test_period_is_unambiguous(self):
        properties = analyzer.ANALYSIS_SCHEMA["properties"]
        assert "fiscal_period" in properties and "calendar_period" in properties

    @pytest.mark.parametrize("schema", [analyzer.ANALYSIS_SCHEMA, analyzer.MEMO_SCHEMA])
    def test_quotes_record_where_they_were_said(self, schema):
        key = "key_quotes" if "key_quotes" in schema["properties"] else "quotes"
        context = schema["properties"][key]["items"]["properties"]["context"]
        assert context["enum"] == ["Prepared remarks", "Q&A", "Filing text"]

    def test_memo_cases_carry_an_implication(self):
        for case in ("bull_case", "bear_case"):
            columns = analyzer.MEMO_SCHEMA["properties"][case]["items"]["properties"]
            assert "implication" in columns, case

    def test_nothing_claims_to_know_the_price(self):
        # The tool has no market data; the prompts must not imply a view on the shares.
        for prompt in (analyzer.ANALYSIS_SYSTEM, analyzer.MEMO_SYSTEM, analyzer.COMPARISON_SYSTEM):
            assert "price target" not in prompt.lower() or "no valuation" in prompt.lower()
        assert "no price or valuation data" in analyzer.ANALYSIS_SYSTEM
        assert "must not imply one" in analyzer.MEMO_SYSTEM
        assert "No valuation, price target" in analyzer.MEMO_SYSTEM
