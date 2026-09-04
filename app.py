"""
Rival Edge — Flask application.

Routes:
    GET  /              the single-page UI
    POST /api/analyze   analyze one document        -> JSON
    POST /api/compare   compare two periods         -> JSON
    POST /api/export    render a summary as a PDF   -> application/pdf
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from typing import Any, Dict, List

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from analyzer import AnalyzerError, analyze_document, compare_documents, extract_text_from_pdf

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload ceiling

ALLOWED_EXTENSIONS = {".txt", ".pdf"}

# Brand palette, shared with static/style.css
NAVY = (11, 27, 43)
SLATE = (46, 63, 82)
INK = (26, 32, 44)
MUTED = (110, 124, 140)
RULE = (214, 221, 229)
ACCENT = (16, 138, 129)
BULLISH = (16, 138, 129)
BEARISH = (183, 60, 60)
NEUTRAL = (176, 137, 46)


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------


def read_document(text_field: str, file_field: str) -> str:
    """Pull document text from either a pasted textarea or an uploaded file."""
    upload = request.files.get(file_field)

    if upload and upload.filename:
        extension = os.path.splitext(upload.filename)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise AnalyzerError(f"Unsupported file type '{extension}'. Upload a .txt or .pdf.")
        if extension == ".pdf":
            return extract_text_from_pdf(io.BytesIO(upload.read()))
        try:
            return upload.read().decode("utf-8", errors="replace")
        except Exception:
            raise AnalyzerError("That .txt file could not be decoded. Save it as UTF-8 and retry.")

    pasted = (request.form.get(text_field) or "").strip()
    if pasted:
        return pasted

    raise AnalyzerError("No document provided. Paste text or upload a .txt or .pdf file.")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html", year=datetime.now().year)


@app.post("/api/analyze")
def api_analyze():
    try:
        document = read_document("text", "file")
        return jsonify(analyze_document(document))
    except AnalyzerError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/compare")
def api_compare():
    try:
        earlier = read_document("text_a", "file_a")
        later = read_document("text_b", "file_b")
        return jsonify(compare_documents(earlier, later))
    except AnalyzerError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/export")
def api_export():
    payload = request.get_json(silent=True) or {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return jsonify({"error": "Nothing to export."}), 400

    if payload.get("mode") == "compare":
        pdf_bytes = build_comparison_pdf(data)
        company = data.get("later", {}).get("company_name", "company")
        filename = f"rival-edge-comparison-{slugify(company)}.pdf"
    else:
        pdf_bytes = build_analysis_pdf(data)
        filename = f"rival-edge-{slugify(data.get('company_name', 'analysis'))}.pdf"

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "That file is larger than the 25 MB limit."}), 413


@app.errorhandler(500)
def server_error(_):
    return jsonify({"error": "Something went wrong on the server. Check the console for details."}), 500


# --------------------------------------------------------------------------
# PDF report
# --------------------------------------------------------------------------


def slugify(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in cleaned.split("-") if part)[:60] or "report"


def latin1(value: Any) -> str:
    """
    fpdf2's core fonts are Latin-1 only. Map the typographic characters that
    show up constantly in filings, then drop anything still unrepresentable.
    """
    text = str(value)
    for source, target in (
        ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
        ("–", "-"), ("—", "-"), ("…", "..."), ("•", "-"),
        (" ", " "), ("−", "-"), ("­", ""),
    ):
        text = text.replace(source, target)
    return text.encode("latin-1", "replace").decode("latin-1")


class Report(FPDF):
    """A4 report with a navy masthead and a running footer."""

    def __init__(self, subtitle: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.subtitle = subtitle
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(18, 16, 18)

    def header(self):
        self.set_fill_color(*NAVY)
        self.rect(0, 0, 210, 26, style="F")

        self.set_xy(18, 8)
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(255, 255, 255)
        self.cell(60, 6, text="RIVAL EDGE", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.set_x(18)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(150, 172, 192)
        self.cell(120, 5, text=latin1(self.subtitle))

        self.set_xy(-70, 12)
        self.set_font("Helvetica", "", 8)
        self.cell(52, 5, text=datetime.now().strftime("%d %B %Y"), align="R")

        self.set_y(38)
        self.set_text_color(*INK)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(
            0, 5,
            text="Generated by Rival Edge. AI-generated analysis for research support only - "
                 "not investment advice.",
        )
        self.set_x(-28)
        self.cell(10, 5, text=str(self.page_no()), align="R")

    # -- building blocks ---------------------------------------------------

    def title_block(self, company: str, meta_line: str):
        self.set_font("Helvetica", "B", 20)
        self.set_text_color(*NAVY)
        self.multi_cell(0, 9, text=latin1(company), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 5, text=latin1(meta_line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(3)

    def sentiment_badge(self, sentiment: str, reasoning: str):
        color = {"Bullish": BULLISH, "Bearish": BEARISH}.get(sentiment, NEUTRAL)
        label = sentiment.upper()
        width = self.get_string_width(label) + 12

        self.set_fill_color(*color)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 9)
        self.cell(width, 7, text=latin1(label), align="C", fill=True,
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.ln(2)
        self.set_font("Helvetica", "I", 9.5)
        self.set_text_color(*SLATE)
        self.multi_cell(0, 5, text=latin1(reasoning), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(4)

    def section(self, heading: str):
        self.ln(2)
        self.set_font("Helvetica", "B", 8.5)
        self.set_text_color(*ACCENT)
        self.cell(0, 5, text=latin1(heading.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*RULE)
        self.line(self.l_margin, self.get_y(), 210 - self.r_margin, self.get_y())
        self.ln(2)

    def body(self, text: str):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(*INK)
        self.multi_cell(0, 5.4, text=latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def bullets(self, items: List[str]):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(*INK)
        for index, item in enumerate(items, start=1):
            self.set_x(self.l_margin)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*ACCENT)
            self.cell(6, 5.4, text=f"{index}.")
            self.set_font("Helvetica", "", 10)
            self.set_text_color(*INK)
            self.multi_cell(0, 5.4, text=latin1(item), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(0.5)
        self.ln(1)

    def quotes(self, items: List[Dict[str, str]]):
        for item in items:
            top = self.get_y()
            self.set_x(self.l_margin + 4)
            self.set_font("Helvetica", "I", 10)
            self.set_text_color(*SLATE)
            self.multi_cell(0, 5.4, text=latin1(f'"{item.get("quote", "")}"'),
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_x(self.l_margin + 4)
            self.set_font("Helvetica", "B", 8.5)
            self.set_text_color(*MUTED)
            self.multi_cell(0, 5, text=latin1(f'- {item.get("speaker", "Unattributed")}'),
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            self.set_draw_color(*ACCENT)
            self.set_line_width(0.8)
            self.line(self.l_margin, top + 1, self.l_margin, self.get_y() - 1)
            self.set_line_width(0.2)
            self.ln(2)


def build_analysis_pdf(analysis: Dict[str, Any]) -> bytes:
    pdf = Report(subtitle="AI financial document analysis")
    pdf.add_page()

    meta = " | ".join(
        part for part in (
            analysis.get("document_type"),
            analysis.get("reporting_period"),
        ) if part
    )
    pdf.title_block(analysis.get("company_name", "Unknown issuer"), meta)
    pdf.sentiment_badge(
        analysis.get("sentiment", "Neutral"),
        analysis.get("sentiment_reasoning", ""),
    )

    pdf.section("Analyst summary")
    pdf.body(analysis.get("analyst_summary", ""))

    pdf.section("Company overview")
    pdf.body(analysis.get("company_overview", ""))

    pdf.section("Revenue and earnings performance")
    pdf.body(analysis.get("revenue_performance", ""))

    pdf.section("Year-over-year growth")
    pdf.body(analysis.get("yoy_growth", ""))

    pdf.section("Management guidance and outlook")
    pdf.body(analysis.get("management_guidance", ""))

    pdf.section("Top risks")
    pdf.bullets(analysis.get("top_risks", []))

    pdf.section("Top opportunities")
    pdf.bullets(analysis.get("top_opportunities", []))

    pdf.section("Key executive quotes")
    pdf.quotes(analysis.get("key_quotes", []))

    return bytes(pdf.output())


def build_comparison_pdf(payload: Dict[str, Any]) -> bytes:
    earlier = payload.get("earlier", {})
    later = payload.get("later", {})
    comparison = payload.get("comparison", {})

    pdf = Report(subtitle="Period-over-period comparison")
    pdf.add_page()

    pdf.title_block(
        later.get("company_name", earlier.get("company_name", "Unknown issuer")),
        f"{earlier.get('reporting_period', 'Earlier period')}  ->  "
        f"{later.get('reporting_period', 'Later period')}",
    )

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*NAVY)
    pdf.multi_cell(0, 6, text=latin1(comparison.get("headline", "")),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.sentiment_badge(
        comparison.get("trajectory", "Mixed"),
        comparison.get("sentiment_shift", ""),
    )

    pdf.section("What changed")
    col_widths = (34, 52, 60, 28)
    headers = ("Metric", earlier.get("reporting_period", "Earlier"),
               later.get("reporting_period", "Later"), "Direction")

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    for width, header in zip(col_widths, headers):
        pdf.cell(width, 7, text=latin1(header)[:28], border=0, fill=True)
    pdf.ln(7)

    pdf.set_text_color(*INK)
    for row_index, delta in enumerate(comparison.get("deltas", [])):
        cells = (
            delta.get("metric", ""),
            delta.get("earlier", ""),
            delta.get("later", ""),
            delta.get("direction", "").upper(),
        )
        commentary = latin1(delta.get("commentary", ""))

        # Row height is driven by the tallest wrapped column, plus the commentary line.
        pdf.set_font("Helvetica", "", 8.5)
        line_counts = [
            len(pdf.multi_cell(width, 4.5, text=latin1(value), dry_run=True, output="LINES"))
            for width, value in zip(col_widths, cells)
        ]
        pdf.set_font("Helvetica", "I", 8)
        commentary_lines = len(
            pdf.multi_cell(sum(col_widths) - 4, 4, text=commentary, dry_run=True, output="LINES")
        ) if commentary else 0
        height = max(line_counts) * 4.5 + commentary_lines * 4 + 3

        if pdf.get_y() + height > pdf.h - pdf.b_margin:
            pdf.add_page()

        if row_index % 2 == 0:
            pdf.set_fill_color(245, 247, 250)
            pdf.rect(pdf.l_margin, pdf.get_y(), sum(col_widths), height, style="F")

        top, left = pdf.get_y(), pdf.l_margin
        for column, (width, value) in enumerate(zip(col_widths, cells)):
            pdf.set_xy(left, top + 1)
            pdf.set_font("Helvetica", "B" if column == 0 else "", 8.5)
            if column == 3:
                direction = delta.get("direction")
                pdf.set_text_color(*{"up": BULLISH, "down": BEARISH}.get(direction, MUTED))
            else:
                pdf.set_text_color(*INK)
            pdf.multi_cell(width, 4.5, text=latin1(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            left += width

        if commentary:
            pdf.set_xy(pdf.l_margin + 4, top + max(line_counts) * 4.5 + 1)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*MUTED)
            pdf.multi_cell(sum(col_widths) - 4, 4, text=commentary,
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_y(top + height)
        pdf.set_draw_color(*RULE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + sum(col_widths), pdf.get_y())

    pdf.ln(3)
    pdf.set_text_color(*INK)

    pdf.section("Guidance change")
    pdf.body(comparison.get("guidance_change", ""))

    pdf.section("Tone of management")
    pdf.body(comparison.get("tone_change", ""))

    new_risks = comparison.get("new_risks", [])
    if new_risks:
        pdf.section("New risks this period")
        pdf.bullets(new_risks)

    resolved = comparison.get("resolved_risks", [])
    if resolved:
        pdf.section("Risks no longer emphasized")
        pdf.bullets(resolved)

    pdf.section("What to watch next quarter")
    pdf.bullets(comparison.get("what_to_watch", []))

    return bytes(pdf.output())


if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n  Warning: ANTHROPIC_API_KEY is not set.")
        print("  Copy .env.example to .env and add your key, then restart.\n")
    app.run(host="127.0.0.1", port=5000, debug=True)
