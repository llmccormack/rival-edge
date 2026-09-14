"""
Rival Edge — analysis engine.

Wraps the Claude API with the prompting, schema enforcement, and long-document
handling needed to turn a raw earnings-call transcript or 10-K into a
structured equity-research summary.

Public surface:
    analyze_document(text, progress)            -> dict
    compare_documents(text_a, text_b, progress) -> dict
    extract_text_from_pdf(stream)               -> str
    AnalyzerError                                  user-presentable failure
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import anthropic

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

MODEL = "claude-opus-5"
MODEL_LABEL = "Claude Opus 5"

# Server-side refusal fallback: if a safety classifier declines a request, the
# API re-runs it on a fallback model inside the same call instead of returning
# nothing. Set RIVAL_EDGE_FALLBACKS=0 to disable.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Opus 5 thinks by default and thinking tokens count toward max_tokens, so the
# ceiling has to leave room for reasoning over a long filing. Safe to set high
# because every request streams.
MAX_TOKENS = 64_000

# Rough characters-per-token ratio for English prose. Only used to decide
# whether a document needs chunking and to report size — never to truncate.
CHARS_PER_TOKEN = 3.5

# Documents longer than this are condensed section by section before the final
# analysis. Claude Opus 5 has a 1M-token context window, so the limit exists to
# keep latency and cost reasonable, not to fit the model.
SINGLE_PASS_CHAR_LIMIT = 320_000
CHUNK_CHARS = 260_000
CHUNK_OVERLAP = 2_000

# Upper bound on concurrent Claude requests from a single analysis.
MAX_PARALLEL_CALLS = 4

MIN_DOCUMENT_CHARS = 200

Progress = Callable[[str], None]

_client: anthropic.Anthropic | None = None
_client_lock = threading.Lock()


class AnalyzerError(Exception):
    """An error worth showing to the user verbatim."""


def _no_progress(_: str) -> None:
    pass


def fallbacks_enabled() -> bool:
    return os.getenv("RIVAL_EDGE_FALLBACKS", "1") != "0"


def get_client() -> anthropic.Anthropic:
    """Build the client lazily so importing this module never needs a key."""
    global _client
    with _client_lock:
        if _client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise AnalyzerError(
                    "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
                )
            # A full 10-K analysis can legitimately run for several minutes.
            _client = anthropic.Anthropic(timeout=600.0)
        return _client


# --------------------------------------------------------------------------
# Run statistics
# --------------------------------------------------------------------------


@dataclass
class RunStats:
    """Token usage and latency across every Claude call in one user request."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, message: Any) -> None:
        usage = message.usage
        with self._lock:
            self.calls += 1
            self.input_tokens += (
                (usage.input_tokens or 0)
                + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            )
            self.output_tokens += usage.output_tokens or 0
            self.models.add(message.model)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "models": sorted(self.models),
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
        }


# --------------------------------------------------------------------------
# Output schemas
#
# Enforced server-side via output_config.format: the model cannot return prose,
# markdown fences, or a missing field. The response is always JSON matching
# the shape below.
#
# Structured outputs reject array minItems/maxItems values other than 0 or 1,
# so list lengths live in the field descriptions and are enforced in code by
# LIST_LIMITS below instead.
# --------------------------------------------------------------------------


def _string_list(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


ANALYSIS_SCHEMA: dict[str, Any] = {
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
        "top_risks": _string_list("Exactly 3, most material first."),
        "top_opportunities": _string_list("Exactly 3, most material first."),
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
            "description": "Exactly 3 verbatim quotes.",
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

COMPARISON_SCHEMA: dict[str, Any] = {
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
                    "direction": {"type": "string", "enum": ["up", "down", "flat", "unclear"]},
                    "commentary": {"type": "string"},
                },
                "required": ["metric", "earlier", "later", "direction", "commentary"],
                "additionalProperties": False,
            },
            "description": "3 to 6 of the most decision-relevant metrics.",
        },
        "new_risks": _string_list("Up to 4. Empty if none."),
        "resolved_risks": _string_list("Up to 4. Empty if none."),
        "guidance_change": {"type": "string"},
        "tone_change": {
            "type": "string",
            "description": "How management's language changed, with evidence.",
        },
        "what_to_watch": _string_list("Exactly 3."),
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


# Maximum list lengths the UI and PDF are designed around. The model usually
# respects the descriptions, but not always (a comparison once came back with
# 7 deltas), so results are trimmed to these after parsing.
LIST_LIMITS: dict[str, int] = {
    "top_risks": 3,
    "top_opportunities": 3,
    "key_quotes": 3,
    "deltas": 6,
    "new_risks": 4,
    "resolved_risks": 4,
    "what_to_watch": 3,
}


def enforce_list_limits(result: dict[str, Any]) -> dict[str, Any]:
    for key, limit in LIST_LIMITS.items():
        if isinstance(result.get(key), list):
            result[key] = result[key][:limit]
    return result


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

You will receive material for two reporting periods of the same company, \
labelled earlier_period and later_period. Each contains a structured analysis \
and the source document it was produced from. Identify what actually changed: \
the numbers, the guidance, the risk profile, and the tone of management's language.

The analyses are a starting point, not the full record. Check them against the \
source documents, and use details the analyses left out — a figure disclosed \
only in Q&A still counts as disclosed.

Rules:
- Compare like for like. Only cite figures that appear in the material provided.
- Where a metric is not comparable across the two periods, say so explicitly.
- "direction" describes movement from the earlier period to the later period.
- Be blunt about deterioration; do not soften it.

Return only valid JSON, no other text."""


# --------------------------------------------------------------------------
# Claude plumbing
# --------------------------------------------------------------------------


def _stream_message(**request: Any) -> Any:
    """
    Run one Claude request to completion.

    Streams under the hood so long documents never hit an HTTP timeout. Uses the
    beta endpoint when refusal fallbacks are on; the request is otherwise identical.
    """
    client = get_client()
    if fallbacks_enabled():
        stream = client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **request)
    else:
        stream = client.messages.stream(**request)

    with stream as active:
        return active.get_final_message()


def _call_claude(
    system: str,
    user_content: str,
    stats: RunStats,
    schema: dict[str, Any] | None = None,
) -> Any:
    """Send one request and return parsed JSON, or raw text when no schema is given."""
    request: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    if schema is not None:
        request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    try:
        message = _stream_message(**request)
    except anthropic.AuthenticationError as exc:
        raise AnalyzerError(
            "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise AnalyzerError(
            "Rate limited by the Anthropic API. Wait a moment and try again."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise AnalyzerError(
            "Could not reach the Anthropic API. Check your network connection."
        ) from exc
    except anthropic.BadRequestError as exc:
        raise AnalyzerError(f"The API rejected this request: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AnalyzerError(
            f"Anthropic API error ({exc.status_code}). Try again shortly."
        ) from exc

    stats.record(message)

    if message.stop_reason == "refusal":
        raise AnalyzerError(
            "Claude declined to analyze this document. If it is a genuine financial "
            "filing, remove any unrelated content and resubmit."
        )
    if message.stop_reason == "max_tokens":
        raise AnalyzerError("The analysis ran past its length limit. Try a shorter excerpt.")

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise AnalyzerError("Claude returned an empty response. Try again.")

    if schema is None:
        return text

    try:
        return enforce_list_limits(json.loads(text))
    except json.JSONDecodeError as exc:
        raise AnalyzerError("The analysis came back malformed. Try again.") from exc


# --------------------------------------------------------------------------
# Long-document handling
# --------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Tidy whitespace left over from PDF extraction without altering content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def split_into_chunks(text: str) -> list[str]:
    """Split on paragraph boundaries, overlapping slightly so no thought is cut in half."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for paragraph in text.split("\n\n"):
        # One paragraph bigger than a whole chunk (common in scraped PDFs) is
        # hard-split rather than dropped.
        if len(paragraph) > CHUNK_CHARS:
            if current:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            chunks.extend(
                paragraph[i : i + CHUNK_CHARS] for i in range(0, len(paragraph), CHUNK_CHARS)
            )
            continue

        if current and size + len(paragraph) > CHUNK_CHARS:
            chunks.append("\n\n".join(current))
            tail = current[-1][-CHUNK_OVERLAP:]
            current, size = [tail], len(tail)

        current.append(paragraph)
        size += len(paragraph) + 2

    if current:
        chunks.append("\n\n".join(current))

    return [chunk for chunk in chunks if chunk.strip()]


def _condense(chunks: list[str], stats: RunStats, progress: Progress, prefix: str) -> str:
    """Condense each section into analyst notes, in parallel, preserving order."""
    total = len(chunks)
    done = 0
    done_lock = threading.Lock()

    def condense_one(indexed: tuple[int, str]) -> str:
        nonlocal done
        index, chunk = indexed
        note = _call_claude(
            CONDENSE_SYSTEM,
            f"Section {index} of {total} of the document:\n\n<section>\n{chunk}\n</section>",
            stats,
        )
        with done_lock:
            done += 1
            progress(f"{prefix}Condensed section {done} of {total}")
        return f"--- NOTES FROM SECTION {index} OF {total} ---\n{note}"

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, total)) as pool:
        notes = list(pool.map(condense_one, enumerate(chunks, start=1)))

    return "\n\n".join(notes)


def _analyze(
    text: str, stats: RunStats, progress: Progress, prefix: str = ""
) -> tuple[dict[str, Any], str]:
    """Analyze one document. Returns the analysis and the text it was based on:
    the document itself, or the condensed notes for a long one."""
    text = normalize_text(text)
    if len(text) < MIN_DOCUMENT_CHARS:
        raise AnalyzerError(
            "That document is too short to analyze. Paste a full transcript or filing "
            f"(at least {MIN_DOCUMENT_CHARS} characters)."
        )

    meta: dict[str, Any] = {
        "characters": len(text),
        "estimated_tokens": estimate_tokens(text),
        "chunked": False,
        "chunks": 1,
    }
    progress(f"{prefix}Reading {len(text):,} characters")

    document = text
    if len(text) > SINGLE_PASS_CHAR_LIMIT:
        chunks = split_into_chunks(text)
        meta.update(chunked=True, chunks=len(chunks))
        progress(f"{prefix}Long document: condensing {len(chunks)} sections in parallel")
        document = _condense(chunks, stats, progress, prefix)

    progress(f"{prefix}Analyzing with {MODEL_LABEL}")
    analysis = _call_claude(ANALYSIS_SYSTEM, f"<document>\n{document}\n</document>", stats, ANALYSIS_SCHEMA)
    analysis["_meta"] = meta
    return analysis, document


# --------------------------------------------------------------------------
# PDF input
# --------------------------------------------------------------------------


def extract_text_from_pdf(file_stream: Any) -> str:
    """Pull text out of an uploaded PDF. Scanned, image-only PDFs yield nothing."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(file_stream)
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise AnalyzerError(
            "That PDF could not be read. It may be corrupt or password-protected."
        ) from exc
    except Exception as exc:
        raise AnalyzerError(
            "That PDF could not be read. Try exporting it again, or paste the text."
        ) from exc

    text = normalize_text("\n\n".join(pages))
    if len(text) < MIN_DOCUMENT_CHARS:
        raise AnalyzerError(
            "No readable text found in that PDF. It is probably a scanned image; "
            "paste the text directly instead."
        )
    return text


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def analyze_document(text: str, progress: Progress = _no_progress) -> dict[str, Any]:
    """Analyze one financial document and return the structured summary."""
    stats = RunStats()
    analysis, _ = _analyze(text, stats, progress)
    analysis["_meta"]["usage"] = stats.as_dict()
    return analysis


def _period_block(label: str, analysis: dict[str, Any], document: str) -> str:
    public = {key: value for key, value in analysis.items() if not key.startswith("_")}
    return (
        f"<{label}>\n<analysis>\n{json.dumps(public, indent=2, ensure_ascii=False)}\n</analysis>\n"
        f"<document>\n{document}\n</document>\n</{label}>"
    )


def compare_documents(
    text_a: str,
    text_b: str,
    progress: Progress = _no_progress,
) -> dict[str, Any]:
    """
    Analyze two documents from the same company, then compare them.

    text_a is the earlier period and text_b the later one. The two analyses are
    independent, so they run concurrently.
    """
    stats = RunStats()
    progress("Analyzing both periods in parallel")

    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier_job = pool.submit(_analyze, text_a, stats, progress, "Earlier period: ")
        later_job = pool.submit(_analyze, text_b, stats, progress, "Later period: ")
        (earlier, earlier_doc), (later, later_doc) = earlier_job.result(), later_job.result()

    progress("Comparing the two periods")
    comparison = _call_claude(
        COMPARISON_SYSTEM,
        _period_block("earlier_period", earlier, earlier_doc)
        + "\n\n"
        + _period_block("later_period", later, later_doc),
        stats,
        COMPARISON_SCHEMA,
    )

    return {
        "earlier": earlier,
        "later": later,
        "comparison": comparison,
        "_meta": {"usage": stats.as_dict()},
    }
