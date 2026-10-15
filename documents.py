"""
Rival Edge — document ingestion.

Turns an uploaded or fetched file into clean analysis-ready text. Handles the
three shapes filings actually arrive in: plain text, PDF, and the HTML that
SEC EDGAR serves for every modern 10-K and 10-Q.
"""

from __future__ import annotations

import io
import re
from html.parser import HTMLParser
from typing import Any

SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".htm", ".html"}

# Tags whose contents are never readable prose. EDGAR filings are inline XBRL:
# ix:header and ix:hidden carry machine-readable tagging that would otherwise
# land in the text as thousands of stray numbers.
SKIP_TAGS = {"script", "style", "head", "ix:header", "ix:hidden"}

BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "section", "article", "header", "footer",
    "table", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "ul", "ol", "blockquote",
}

CELL_TAGS = {"td", "th"}


class DocumentError(Exception):
    """An ingestion failure worth showing to the user verbatim."""


class _TextExtractor(HTMLParser):
    """Collect readable text, keeping table cells distinguishable."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skipping += 1
            return
        if self._skipping:
            return
        if tag in CELL_TAGS:
            self.parts.append(" | ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
            return
        if self._skipping:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def normalize_text(text: str) -> str:
    """Tidy whitespace from extraction without altering content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    lines = []
    for line in "".join(parser.parts).split("\n"):
        # Collapse the cell separators left by empty layout cells.
        cleaned = re.sub(r"(\s*\|\s*)+", " | ", line).strip().strip("|").strip()
        if cleaned:
            lines.append(cleaned)

    return normalize_text("\n".join(lines))


def extract_text_from_pdf(file_stream: Any) -> str:
    """Pull text out of a PDF. Scanned, image-only PDFs yield nothing."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(file_stream)
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise DocumentError(
            "That PDF could not be read. It may be corrupt or password-protected."
        ) from exc
    except Exception as exc:
        raise DocumentError(
            "That PDF could not be read. Try exporting it again, or paste the text."
        ) from exc

    text = normalize_text("\n\n".join(pages))
    if len(text) < 200:
        raise DocumentError(
            "No readable text found in that PDF. It is probably a scanned image; "
            "paste the text directly instead."
        )
    return text


def extract(filename: str, data: bytes) -> str:
    """Extract text from an uploaded file, choosing the reader by extension."""
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentError(
            f"Unsupported file type '{extension or filename}'. "
            "Upload a .txt, .pdf, .htm or .html file."
        )

    if extension == ".pdf":
        return extract_text_from_pdf(io.BytesIO(data))

    text = data.decode("utf-8", errors="replace")
    if extension in (".htm", ".html"):
        return extract_text_from_html(text)
    return normalize_text(text)
