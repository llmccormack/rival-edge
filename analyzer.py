"""
Rival Edge — analysis engine.

Wraps the Claude API with the prompting, schema enforcement, and long-document
handling needed to turn a raw earnings-call transcript or 10-K into a
structured equity-research summary.

Public surface:
    analyze_document(text, progress)            -> dict
    compare_documents(text_a, text_b, progress) -> dict
    AnalyzerError                                  user-presentable failure

Document ingestion lives in documents.py; filing retrieval in edgar.py.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import anthropic

from documents import normalize_text

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
        "fiscal_period": {
            "type": "string",
            "description": "As the company labels it, e.g. 'Q3 FY2025'. 'Not stated' if absent.",
        },
        "calendar_period": {
            "type": "string",
            "description": (
                "Just the months the fiscal period covers, e.g. 'Jul-Sep 2025'. Under 20 "
                "characters: a label, not a sentence, and no commentary about the call "
                "date. Fiscal years differ by company, so this removes the ambiguity. "
                "'Not stated' if the document does not make it derivable."
            ),
        },
        "company_overview": {
            "type": "string",
            "description": "1-2 sentences on what the company does.",
        },
        "financial_summary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "e.g. 'Revenue', 'Adjusted EPS'."},
                    "current": {"type": "string", "description": "This period, as reported."},
                    "prior": {"type": "string", "description": "Same period last year, or 'Not disclosed'."},
                    "change": {"type": "string", "description": "e.g. '+0.7%', '(4.5)%', '+140 bps'."},
                },
                "required": ["metric", "current", "prior", "change"],
                "additionalProperties": False,
            },
            "description": (
                "4 to 6 rows of the headline financials a PM scans first. Revenue and "
                "earnings per share come first whenever the document discloses them, "
                "including both GAAP and adjusted EPS where both are given: EPS is a "
                "headline number and its absence from this table is a defect. Then the "
                "margin or operating ratio, and the operating driver that matters for "
                "this business. Reported figures only. Where only a change is disclosed "
                "and no level, put it in change and write 'Not disclosed' for current "
                "and prior rather than repeating it across columns."
            ),
        },
        "revenue_performance": {"type": "string"},
        "yoy_growth": {"type": "string"},
        "management_guidance": {"type": "string"},
        "guidance_bridge": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "e.g. 'FY2025 revenue'."},
                    "prior": {
                        "type": "string",
                        "description": (
                            "The previous guide. If the company guided before but this "
                            "document does not restate it, write 'Not stated in this "
                            "document'. Only write 'No prior guide' when the company is "
                            "guiding to this measure for the first time."
                        ),
                    },
                    "current": {"type": "string", "description": "The guide as it now stands, or 'Withdrawn'."},
                    "change": {
                        "type": "string",
                        "description": (
                            "The move at the midpoint, in the figure's own units and as a "
                            "percentage where both ends are given, e.g. '-$200M at midpoint, "
                            "(3.5)%'. Compute it; do not restate the ranges or the current "
                            "midpoint. Where the prior value is unknown this reads 'New "
                            "guide' or 'Not comparable', never 'Raised' or 'Cut'."
                        ),
                    },
                },
                "required": ["metric", "prior", "current", "change"],
                "additionalProperties": False,
            },
            "description": (
                "Every forward number management changed, raised, cut, reaffirmed or "
                "withdrew. Up to 5 rows. Empty only if the document gives no guidance."
            ),
        },
        "top_risks": _string_list("Exactly 3, most material first."),
        "top_opportunities": _string_list("Exactly 3, most material first."),
        "key_quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "quote": {"type": "string"},
                    "context": {
                        "type": "string",
                        "enum": ["Prepared remarks", "Q&A", "Filing text"],
                        "description": (
                            "Where it was said. Q&A answers are unscripted and carry more "
                            "weight than prepared remarks; get this right."
                        ),
                    },
                },
                "required": ["speaker", "quote", "context"],
                "additionalProperties": False,
            },
            "description": "Exactly 3 verbatim quotes. Prefer Q&A over prepared remarks.",
        },
        "disclosure_read": {"type": "string", "enum": ["Bullish", "Neutral", "Bearish"]},
        "disclosure_reason": {"type": "string"},
        "analyst_summary": {"type": "string"},
    },
    "required": [
        "company_name",
        "document_type",
        "fiscal_period",
        "calendar_period",
        "company_overview",
        "financial_summary",
        "revenue_performance",
        "yoy_growth",
        "management_guidance",
        "guidance_bridge",
        "top_risks",
        "top_opportunities",
        "key_quotes",
        "disclosure_read",
        "disclosure_reason",
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
        "disclosure_shift": {
            "type": "string",
            "description": (
                "How the tone of what management disclosed moved between the periods, "
                "with evidence. Not a view on the shares."
            ),
        },
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
        "disclosure_shift",
        "deltas",
        "new_risks",
        "resolved_risks",
        "guidance_change",
        "tone_change",
        "what_to_watch",
    ],
    "additionalProperties": False,
}


MEMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "basis": {
            "type": "string",
            "description": "The documents this memo rests on, e.g. 'Q3 FY2025 earnings call'.",
        },
        "situation": {
            "type": "string",
            "description": (
                "2-3 sentences, under 420 characters: what just happened at this company, "
                "in an analyst's voice."
            ),
        },
        "key_figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short metric name, under 24 characters."},
                    "value": {"type": "string", "description": "The figure itself, under 16 characters."},
                    "period": {
                        "type": "string",
                        "description": "Which period this figure is, e.g. '3Q FY25'. Under 12 characters.",
                    },
                    "change": {"type": "string", "description": "Change versus prior period, or empty."},
                    "direction": {"type": "string", "enum": ["up", "down", "flat", "none"]},
                },
                "required": ["label", "value", "period", "change", "direction"],
                "additionalProperties": False,
            },
            "description": "Exactly 4 figures a portfolio manager would want on one line.",
        },
        "bull_case": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "One line, under 70 characters."},
                    "evidence": {
                        "type": "string",
                        "description": "One sentence citing the document, under 170 characters.",
                    },
                    "implication": {
                        "type": "string",
                        "description": (
                            "What it does to earnings power or the margin structure if it "
                            "holds, under 120 characters. The argument, not the fact. No "
                            "share-price or valuation claim."
                        ),
                    },
                },
                "required": ["claim", "evidence", "implication"],
                "additionalProperties": False,
            },
            "description": "Exactly 3, strongest first.",
        },
        "bear_case": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "One line, under 70 characters."},
                    "evidence": {
                        "type": "string",
                        "description": "One sentence citing the document, under 170 characters.",
                    },
                    "implication": {
                        "type": "string",
                        "description": (
                            "What it does to earnings power or the margin structure if it "
                            "holds, under 120 characters. The argument, not the fact. No "
                            "share-price or valuation claim."
                        ),
                    },
                },
                "required": ["claim", "evidence", "implication"],
                "additionalProperties": False,
            },
            "description": "Exactly 3, strongest first.",
        },
        "catalysts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string", "description": "What happens, under 60 characters."},
                    "timing": {
                        "type": "string",
                        "description": "When, as disclosed, under 26 characters. 'Not dated' if absent.",
                    },
                },
                "required": ["event", "timing"],
                "additionalProperties": False,
            },
            "description": "Exactly 3 dated or datable events from the document.",
        },
        "open_questions": _string_list(
            "Exactly 3 questions this document cannot answer, that need market data, "
            "consensus estimates, or peer comparison. These are what the analyst must do "
            "next. One sentence each, under 170 characters."
        ),
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "quote": {"type": "string", "description": "Verbatim, under 165 characters."},
                    "context": {"type": "string", "enum": ["Prepared remarks", "Q&A", "Filing text"]},
                },
                "required": ["speaker", "quote", "context"],
                "additionalProperties": False,
            },
            "description": "Exactly 2 quotes worth citing aloud in a pitch. Prefer Q&A.",
        },
        "what_would_change_the_read": _string_list(
            "Exactly 2 falsifiers: the specific, observable things that would overturn "
            "the read above. Name the metric and the threshold where the document "
            "supports one, e.g. 'tonnage back above flat by the January guide'."
        ),
        "disclosure_read": {"type": "string", "enum": ["Bullish", "Neutral", "Bearish"]},
        "disclosure_reason": {"type": "string", "description": "One sentence."},
    },
    "required": [
        "company_name",
        "basis",
        "situation",
        "key_figures",
        "bull_case",
        "bear_case",
        "catalysts",
        "open_questions",
        "quotes",
        "what_would_change_the_read",
        "disclosure_read",
        "disclosure_reason",
    ],
    "additionalProperties": False,
}


# Maximum list lengths the UI and PDF are designed around. The model usually
# respects the descriptions, but not always (a comparison once came back with
# 7 deltas), so results are trimmed to these after parsing.
LIST_LIMITS: dict[str, int] = {
    "financial_summary": 6,
    "guidance_bridge": 5,
    "what_would_change_the_read": 2,
    "key_figures": 4,
    "bull_case": 3,
    "bear_case": 3,
    "catalysts": 3,
    "open_questions": 3,
    "quotes": 2,
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

ANALYSIS_SYSTEM = """You are a senior equity research analyst writing the note a \
portfolio manager reads before the stock opens. Analyze the financial document and \
return the JSON object the schema describes.

What matters most, in order:
1. The guidance bridge. What forward numbers changed, from what to what, and by how \
   much at the midpoint. Do the arithmetic for the reader. A withdrawn target is a \
   change: record it as "Withdrawn".
2. The reported financials, as a table a reader can scan, not as prose.
3. What management said under questioning, as opposed to in the script.

Rules:
- Use only figures the COMPANY states or confirms. An analyst asserting a number \
  inside a question is not disclosure: do not carry it into a table. If management \
  confirms it, it is usable and you attribute it to management; if they do not, \
  leave it out.
- Never estimate, extrapolate or invent. Deriving a midpoint or a percentage change \
  from two stated numbers is arithmetic, not estimation: do that.
- If the document does not state something, write "Not disclosed" rather than \
  guessing. In the guidance bridge this matters: a prior guide the company gave but \
  this document does not restate is "Not stated in this document", never "No prior \
  guide", which claims the company never guided.
- A change is a comparison. If the prior value is unknown, the change column reads \
  "New guide" or "Not comparable". Never write "Raised" or "Cut" against an unknown \
  prior, and never restate the current midpoint as though it were the change.
- Quotes are verbatim and attributed, and the context field must be right: an answer \
  in the Q&A session is unscripted and carries more weight than prepared remarks.
- Keep units consistent within a field. Use the company's own convention for the \
  figure, and write negatives in parentheses, as (4.3)%, the way a note does.
- Label the basis whenever it is not plain reported: constant currency as "cc", \
  organic, adjusted, pro forma. Where the company gives both reported and constant \
  currency, give both rather than silently choosing one, because a reported level \
  next to a constant-currency growth rate does not reconcile.
- State the calendar months behind the fiscal label whenever the document makes them \
  derivable. Fiscal years differ by company and the reader cannot be expected to know.
- disclosure_read describes the tone of what management disclosed. It is not a view \
  on the shares, and you have no price or valuation data, so never imply one.
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


MEMO_SYSTEM = """You are a senior equity research analyst drafting the company \
section of a one-page investment memo, for an analyst who will pitch this name.

You will receive a structured analysis and the source document it came from.
Write the part of the memo that the company's own disclosure can support.

Rules:
- Every claim rests on the document. Cite the figure or the language that supports it.
- Use only figures the company states or confirms. A number an analyst puts into a \
  question is not disclosure unless management confirms it.
- Never estimate, extrapolate, or invent a number.
- Label the basis where it is not plain reported: cc, organic, adjusted, pro forma.
- No valuation, price target, multiple, or recommendation to buy or sell. You do \
  not know the share price and must not imply one.
- The bull and bear cases are what the disclosure itself supports on each side, \
  not a balanced-sounding pair. If the evidence leans one way, say so in the \
  disclosure_read and let the weaker side be thin.
- A case is an argument, not a list of events. "Last-mile turned profitable" is a \
  fact; the implication field is where you say what it does to the margin structure \
  if it holds. Tie every claim to earnings power, never to a share price.
- what_would_change_the_read are falsifiers a reader can actually watch for: a \
  metric and a level, taken from what the company disclosed or guided.
- open_questions are the things this document cannot settle and the analyst must \
  check elsewhere: valuation, consensus, peer comparison, market share. Make them \
  specific to this company, not generic research advice.
- Quotes are verbatim and attributed, and the context field must be right: an \
  unscripted Q&A answer carries more weight than prepared remarks, so prefer it.
- Keep units consistent, and write negatives in parentheses, as (4.3)%.
- Write for someone who will say these words out loud. Short sentences, real \
  numbers, no filler.
- This is a one-page memo. Respect every length limit in the schema; a long \
  answer is a worse answer here.

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


def draft_memo(
    result: dict[str, Any],
    documents: list[str],
    progress: Progress = _no_progress,
) -> dict[str, Any]:
    """
    Draft the disclosure-backed half of a one-page pitch memo.

    `result` is an analysis or a comparison already produced by this module, and
    `documents` are the source texts behind it. Both go to the model: the
    analysis for structure, the source for detail the summary left out.
    """
    stats = RunStats()
    progress("Drafting the memo")

    public = {key: value for key, value in result.items() if not key.startswith("_")}
    if "comparison" in public:
        for period in ("earlier", "later"):
            public[period] = {
                k: v for k, v in public.get(period, {}).items() if not k.startswith("_")
            }

    sources = "\n\n".join(
        f"<document index=\"{index}\">\n{text}\n</document>"
        for index, text in enumerate(documents, start=1)
        if text
    )
    memo = _call_claude(
        MEMO_SYSTEM,
        f"<analysis>\n{json.dumps(public, indent=2, ensure_ascii=False)}\n</analysis>\n\n{sources}",
        stats,
        MEMO_SCHEMA,
    )
    memo["_meta"] = {"usage": stats.as_dict()}
    return memo


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
