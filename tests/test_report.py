"""PDF report generation."""

from __future__ import annotations

import io

from pypdf import PdfReader

import fixtures
import report


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def test_analysis_report_contains_every_section():
    text = pdf_text(report.build_analysis_pdf(fixtures.analysis()))
    for heading in (
        "ANALYST SUMMARY", "COMPANY OVERVIEW", "REVENUE AND EARNINGS PERFORMANCE",
        "YEAR-OVER-YEAR GROWTH", "MANAGEMENT GUIDANCE", "TOP RISKS", "TOP OPPORTUNITIES",
        "KEY EXECUTIVE QUOTES",
    ):
        assert heading in text
    assert "Testco Freight, Inc." in text
    assert "BULLISH" in text


def test_comparison_report_includes_row_commentary():
    text = pdf_text(report.build_comparison_pdf(fixtures.comparison_payload()))
    assert "WHAT CHANGED" in text
    assert "First profitable quarter for the segment." in text
    assert "DETERIORATING" in text


def test_typographic_and_non_latin_characters_do_not_crash():
    data = fixtures.analysis()
    data["analyst_summary"] = "Curly “quotes”, an em—dash, an ellipsis…, € and 日本語."
    text = pdf_text(report.build_analysis_pdf(data))
    assert '"quotes"' in text


def test_long_comparison_table_breaks_across_pages():
    payload = fixtures.comparison_payload()
    row = payload["comparison"]["deltas"][0]
    payload["comparison"]["deltas"] = [
        {**row, "commentary": "A long explanation of the change. " * 12} for _ in range(12)
    ]
    reader = PdfReader(io.BytesIO(report.build_comparison_pdf(payload)))
    assert len(reader.pages) > 1


def test_slugify():
    assert report.slugify("Northwind Logistics, Inc.") == "northwind-logistics-inc"
    assert report.slugify("!!!") == "report"


def test_paragraphs_are_left_aligned_not_justified():
    # fpdf2 justifies multi_cell text by default, which stretches word spacing
    # badly in narrow table cells. Every call must opt into left alignment.
    import inspect

    source = inspect.getsource(report)
    assert source.count("multi_cell(") == source.count('align="L"')


def _contrast(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    def luminance(rgb):
        channels = [c / 255 for c in rgb]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_report_text_colors_meet_wcag_aa():
    white = (255, 255, 255)
    for name in ("INK", "SLATE", "MUTED", "ACCENT", "BULLISH", "BEARISH", "NEUTRAL"):
        assert _contrast(getattr(report, name), white) >= 4.5, name
