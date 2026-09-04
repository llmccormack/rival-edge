"""
Rival Edge — analysis engine.

Wraps the Claude API with the prompting, schema enforcement, and long-document
handling needed to turn a raw earnings-call transcript or 10-K into a
structured equity-research summary.

Public surface:
    analyze_document(text)            -> dict   (single-document analysis)
    compare_documents(text_a, text_b) -> dict   (two-period comparison)
    extract_text_from_pdf(stream)     -> str
    AnalyzerError                              (user-presentable failure)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

import anthropic

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

MODEL = "claude-opus-5"

# Server-side refusal fallback: if a safety classifier declines the request,
# the API transparently re-runs it on a fallback model inside the same call
# instead of returning an empty result. Set RIVAL_EDGE_FALLBACKS=0 to disable.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
USE_FALLBACKS = os.getenv("RIVAL_EDGE_FALLBACKS", "1") != "0"

MAX_TOKENS = 8_000

# A rough characters-per-token ratio for English prose. Only used to decide
# whether a document needs chunking — never to truncate content.
CHARS_PER_TOKEN = 3.5

# Documents longer than this are condensed chunk-by-chunk before the final
# analysis pass. Claude Opus 5 has a 1M-token context window, so this limit is
# about keeping latency and cost sane, not about fitting the model.
SINGLE_PASS_CHAR_LIMIT = 320_000
CHUNK_CHARS = 260_000
CHUNK_OVERLAP = 2_000

MIN_DOCUMENT_CHARS = 200

_client: anthropic.Anthropic | None = None


class AnalyzerError(Exception):
    """An error worth showing to the user verbatim."""


def get_client() -> anthropic.Anthropic:
    """Lazily build the Anthropic client so import never fails on a missing key."""
    global _client
    if _client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise AnalyzerError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        # Analysis of a full 10-K can legitimately run for several minutes.
        _client = anthropic.Anthropic(timeout=600.0)
    return _client


# --------------------------------------------------------------------------
# Output schemas
#
# These are enforced server-side via output_config.format, so the model cannot
# return prose, markdown fences, or a missing field — the response is always
# JSON matching the shape below.
# --------------------------------------------------------------------------

ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "document_type": {
            "type": "string",
            "description": "earnings call, 10-K, or the closest accurate label",
        },
        "reporting_period": {
            "type": "string",
            "description": "e.g. 'Q3 FY2025' or 'FY2024'. 'Not stated' if absent.",
        },
        "company_overview": {
            "type": "string",
            "description": "1-2 sentences on what the company does.",
        },
        "revenue_performance": {"type": "string"},
        "yoy_growth": {"type": "string"},
        "management_guidance": {"type": "string"},
        "top_risks": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
        "top_opportunities": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
        "key_quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["speaker", "quote"],
                "additionalProperties": False,
            },
            "minItems": 3,
            "maxItems": 3,
        },
        "sentiment": {"type": "string", "enum": ["Bullish", "Neutral", "Bearish"]},
        "sentiment_reasoning": {"type": "string"},
        "analyst_summary": {"type": "string"},
    },
    "required": [
        "company_name",
        "document_type",
        "reporting_period",
        "company_overview",
        "revenue_performance",
        "yoy_growth",
        "management_guidance",
        "top_risks",
        "top_opportunities",
        "key_quotes",
        "sentiment",
        "sentiment_reasoning",
        "analyst_summary",
    ],
    "additionalProperties": False,
}

COMPARISON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence on the single most important change between periods.",
        },
        "trajectory": {
            "type": "string",
            "enum": ["Improving", "Stable", "Deteriorating", "Mixed"],
        },
        "sentiment_shift": {"type": "string"},
        "deltas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "earlier": {"type": "string"},
                    "later": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "flat", "unclear"],
                    },
                    "commentary": {"type": "string"},
                },
                "required": ["metric", "earlier", "later", "direction", "commentary"],
                "additionalProperties": False,
            },
            "minItems": 3,
            "maxItems": 6,
        },
        "new_risks": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "resolved_risks": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "guidance_change": {"type": "string"},
        "tone_change": {
            "type": "string",
            "description": "How management's language changed, with evidence.",
        },
        "what_to_watch": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
    },
    "required": [
        "headline",
        "trajectory",
        "sentiment_shift",
        "deltas",
        "new_risks",
        "resolved_risks",
        "guidance_change",
        "tone_change",
        "what_to_watch",
    ],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

ANALYSIS_SYSTEM = """You are a senior equity research analyst. Analyze the \
financial document you are given and return a JSON object with these exact fields:
- company_name
- document_type (earnings call or 10-K)
- reporting_period
- company_overview (1-2 sentences on what the company does)
- revenue_performance (string summary with numbers)
- yoy_growth (string)
- management_guidance (string)
- top_risks (array of 3 strings)
- top_opportunities (array of 3 strings)
- key_quotes (array of 3 objects with speaker and quote fields)
- sentiment (Bullish, Neutral, or Bearish)
- sentiment_reasoning (1-2 sentences)
- analyst_summary (3-4 sentence overall summary written like an analyst note)

Rules:
- Use only figures that appear in the document. Never estimate or invent a number.
- If the document does not state something, say "Not disclosed" rather than guessing.
- key_quotes must be verbatim from the document, attributed to the named speaker.
- Write for a portfolio manager: specific, quantitative, no filler.

Return only valid JSON, no other text."""

CONDENSE_SYSTEM = """You are a research associate preparing notes for a senior \
equity analyst. You will receive one section of a long financial document.

Extract everything that would matter to an analyst, preserving exact figures, \
percentages, dates, guidance ranges, risk-factor language, and verbatim \
executive quotes with their speakers. Drop boilerplate, legal disclaimers, \
tables of contents, and repetition.

Write dense factual notes, not prose. Do not analyze, editorialize, or draw \
conclusions — later stages do that. Never invent a figure that is not present."""

COMPARISON_SYSTEM = """You are a senior equity research analyst writing a \
quarter-over-quarter comparison note.

You will receive two structured analyses of the same company from two different \
reporting periods, labelled EARLIER PERIOD and LATER PERIOD. Identify what \
actually changed: the numbers, the guidance, the risk profile, and the tone of \
management's language.

Rules:
- Compare like for like. Only cite figures that appear in the analyses provided.
- Where a metric is not comparable across the two periods, say so explicitly.
- "direction" describes movement from the earlier period to the later period.
- Be blunt about deterioration; do not soften it.

Return only valid JSON, no other text."""


# --------------------------------------------------------------------------
# Claude plumbing
# --------------------------------------------------------------------------


def _stream_message(**kwargs: Any):
    """
    Run one Claude request, streaming so long documents never hit an HTTP timeout.

    Uses the beta endpoint when refusal fallbacks are enabled, the stable one
    otherwise; the request body is identical apart from the fallback parameters.
    """
    client = get_client()
    if USE_FALLBACKS:
        stream_ctx = client.beta.messages.stream(
            betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
    else:
        stream_ctx = client.messages.stream(**kwargs)

    with stream_ctx as stream:
        return stream.get_final_message()


def _call_claude(system: str, user_content: str, schema: Dict[str, Any] | None = None) -> Any:
    """Send one request to Claude and return parsed JSON (or raw text if no schema)."""
    request: Dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    if schema is not None:
        request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    try:
        message = _stream_message(**request)
    except anthropic.AuthenticationError:
        raise AnalyzerError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.RateLimitError:
        raise AnalyzerError("Rate limited by the Anthropic API. Wait a moment and try again.")
    except anthropic.APIConnectionError:
        raise AnalyzerError("Could not reach the Anthropic API. Check your network connection.")
    except anthropic.BadRequestError as exc:
        raise AnalyzerError(f"The API rejected this request: {exc.message}")
    except anthropic.APIStatusError as exc:
        raise AnalyzerError(f"Anthropic API error ({exc.status_code}). Try again shortly.")

    if message.stop_reason == "refusal":
        raise AnalyzerError(
            "Claude declined to analyze this document. If it is a genuine financial "
            "filing, try removing any unrelated content and resubmitting."
        )

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise AnalyzerError("Claude returned an empty response. Try again.")

    if schema is None:
        return text

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # output_config.format guarantees valid JSON, so this only fires if the
        # response was cut short by max_tokens.
        raise AnalyzerError("The analysis came back incomplete. Try a shorter excerpt.")


# --------------------------------------------------------------------------
# Long-document handling
# --------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def _split_into_chunks(text: str) -> List[str]:
    """Split on paragraph boundaries, with a small overlap so context isn't cut mid-thought."""
    paragraphs = text.split("\n\n")
    chunks: List[str] = []
    current: List[str] = []
    size = 0

    for paragraph in paragraphs:
        # A single paragraph larger than a whole chunk (common in scraped PDFs)
        # gets hard-split rather than skipped.
        if len(paragraph) > CHUNK_CHARS:
            if current:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            for i in range(0, len(paragraph), CHUNK_CHARS):
                chunks.append(paragraph[i : i + CHUNK_CHARS])
            continue

        if size + len(paragraph) > CHUNK_CHARS and current:
            chunks.append("\n\n".join(current))
            tail = current[-1][-CHUNK_OVERLAP:] if current else ""
            current, size = ([tail] if tail else []), len(tail)

        current.append(paragraph)
        size += len(paragraph) + 2

    if current:
        chunks.append("\n\n".join(current))

    return [chunk for chunk in chunks if chunk.strip()]


def _condense(text: str) -> str:
    """Map/reduce a document too long to analyze in one pass into analyst notes."""
    chunks = _split_into_chunks(text)
    notes: List[str] = []

    for index, chunk in enumerate(chunks, start=1):
        note = _call_claude(
            CONDENSE_SYSTEM,
            f"Section {index} of {len(chunks)} of the document:\n\n<section>\n{chunk}\n</section>",
        )
        notes.append(f"--- NOTES FROM SECTION {index} OF {len(chunks)} ---\n{note}")

    return "\n\n".join(notes)


def prepare_document(text: str) -> tuple[str, Dict[str, Any]]:
    """
    Return (document_to_analyze, metadata).

    Short documents pass straight through. Long ones are condensed first, and
    the metadata records that so the UI can disclose it.
    """
    text = normalize_text(text)
    if len(text) < MIN_DOCUMENT_CHARS:
        raise AnalyzerError(
            "That document is too short to analyze. Paste a full transcript or filing "
            f"(at least {MIN_DOCUMENT_CHARS} characters)."
        )

    meta: Dict[str, Any] = {
        "characters": len(text),
        "estimated_tokens": estimate_tokens(text),
        "chunked": False,
        "chunks": 1,
    }

    if len(text) <= SINGLE_PASS_CHAR_LIMIT:
        return text, meta

    chunk_count = len(_split_into_chunks(text))
    meta.update(chunked=True, chunks=chunk_count)
    return _condense(text), meta


def normalize_text(text: str) -> str:
    """Tidy whitespace from PDF extraction without altering content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# PDF input
# --------------------------------------------------------------------------


def extract_text_from_pdf(file_stream) -> str:
    """Pull text out of an uploaded PDF. Scanned/image-only PDFs yield nothing."""
    from PyPDF2 import PdfReader
    from PyPDF2.errors import PdfReadError

    try:
        reader = PdfReader(file_stream)
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError:
        raise AnalyzerError("That PDF could not be read. It may be corrupt or password-protected.")
    except Exception:
        raise AnalyzerError("That PDF could not be read. Try exporting it again or paste the text.")

    text = normalize_text("\n\n".join(pages))
    if len(text) < MIN_DOCUMENT_CHARS:
        raise AnalyzerError(
            "No readable text found in that PDF. It is probably a scanned image — "
            "paste the text directly instead."
        )
    return text


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def analyze_document(text: str) -> Dict[str, Any]:
    """Analyze one financial document and return the structured summary."""
    document, meta = prepare_document(text)
    analysis = _call_claude(
        ANALYSIS_SYSTEM,
        f"<document>\n{document}\n</document>",
        ANALYSIS_SCHEMA,
    )
    analysis["_meta"] = meta
    return analysis


def compare_documents(text_a: str, text_b: str) -> Dict[str, Any]:
    """
    Analyze two documents from the same company and compare them.

    text_a is treated as the earlier period, text_b as the later one.
    """
    earlier = analyze_document(text_a)
    later = analyze_document(text_b)

    payload = {
        "earlier_period": {k: v for k, v in earlier.items() if not k.startswith("_")},
        "later_period": {k: v for k, v in later.items() if not k.startswith("_")},
    }
    comparison = _call_claude(
        COMPARISON_SYSTEM,
        json.dumps(payload, indent=2),
        COMPARISON_SCHEMA,
    )

    return {"earlier": earlier, "later": later, "comparison": comparison}
