"""
Hand-written test data shaped like real analyzer output.

Used only by the test suite. The browsable examples in samples/ come from real
Claude runs (scripts/generate_examples.py), never from here.
"""

from __future__ import annotations

import copy

ANALYSIS = {
    "company_name": "Testco Freight, Inc.",
    "document_type": "earnings call",
    "reporting_period": "Q2 FY2025",
    "company_overview": "Testco operates a regional freight network with a last-mile delivery arm.",
    "revenue_performance": "Revenue of $1.42B, up from $1.31B. Adjusted EPS of $1.86.",
    "yoy_growth": "Revenue +8.4% YoY; adjusted EPS +11.4% YoY — growth was price-led.",
    "management_guidance": "FY revenue guidance raised to $5.75–5.85B from $5.65–5.80B.",
    "top_risks": [
        "Industrial demand softening, with June tonnage down 2%.",
        "Driver wage inflation of 6% running ahead of 5.2% yield growth.",
        "Top ten customers rose to 31% of revenue from 27%.",
    ],
    "top_opportunities": [
        "Last-mile revenue grew 24% and is approaching breakeven.",
        "Two Texas terminals open a $600M addressable market.",
        "Contract renewals repricing at high-single-digit increases.",
    ],
    "key_quotes": [
        {"speaker": "Dana Whitfield, CEO", "quote": "We would rather concede volume than concede yield."},
        {"speaker": "Marcus Lee, CFO", "quote": "We see a path to the mid-87s next year."},
        {"speaker": "Dana Whitfield, CEO", "quote": "Industrial demand is clearly softer than it was ninety days ago."},
    ],
    "sentiment": "Bullish",
    "sentiment_reasoning": "Beat-and-raise quarter tempered by explicit caution on industrial demand.",
    "analyst_summary": "A clean, price-led beat and raise with a record operating ratio. "
    "Last-mile is scaling toward profitability. Demand is the offset.",
}

COMPARISON = {
    "headline": "A beat-and-raise quarter became a miss-and-cut as tonnage fell 4.3%.",
    "trajectory": "Deteriorating",
    "sentiment_shift": "Bullish to Bearish.",
    "deltas": [
        {
            "metric": "Revenue",
            "earlier": "$1.42B (+8.4% YoY)",
            "later": "$1.39B (+0.7% YoY)",
            "direction": "down",
            "commentary": "Growth decelerated sharply in one quarter.",
        },
        {
            "metric": "Operating ratio",
            "earlier": "88.4%",
            "later": "89.8%",
            "direction": "down",
            "commentary": "Volume deleverage on a fixed network.",
        },
        {
            "metric": "Last-mile margin",
            "earlier": "-1.8%",
            "later": "+0.4%",
            "direction": "up",
            "commentary": "First profitable quarter for the segment.",
        },
    ],
    "new_risks": ["Yield no longer offsets volume declines."],
    "resolved_risks": [],
    "guidance_change": "FY revenue cut by $200M at the midpoint; capex reduced to $360M.",
    "tone_change": "From “pricing with discipline” to “we are not calling a bottom.”",
    "what_to_watch": [
        "Whether October tonnage stabilizes.",
        "Whether renewals hold mid-single-digit increases.",
        "Whether last-mile stays profitable.",
    ],
}


def analysis() -> dict:
    return copy.deepcopy(ANALYSIS)


def comparison_payload() -> dict:
    later = copy.deepcopy(ANALYSIS)
    later.update(reporting_period="Q3 FY2025", sentiment="Bearish")
    return {"earlier": analysis(), "later": later, "comparison": copy.deepcopy(COMPARISON)}


MEMO = {
    "company_name": "Testco Freight, Inc.",
    "basis": "Q3 FY2025 earnings call",
    "situation": "Testco cut full-year guidance one quarter after raising it, as tonnage "
    "fell 4.3% and the operating ratio worsened 140 basis points to 89.8%.",
    "key_figures": [
        {"label": "Revenue", "value": "$1.39B", "change": "+0.7% YoY", "direction": "up"},
        {"label": "Adjusted EPS", "value": "$1.71", "change": "-4.5% YoY", "direction": "down"},
        {"label": "Operating ratio", "value": "89.8%", "change": "+140 bps YoY", "direction": "up"},
        {"label": "Tonnage per day", "value": "-4.3%", "change": "Sept -5.6%", "direction": "down"},
    ],
    "bull_case": [
        {"claim": "Pricing held at 5.1% ex-fuel yield", "evidence": "Whitfield said revenue per "
         "hundredweight excluding fuel rose 5.1% even after walking away from $40M of revenue."},
        {"claim": "Last-mile turned profitable", "evidence": "Last-mile revenue was $204M, up 19%, "
         "at a 0.4% operating margin, the segment's first profitable quarter."},
        {"claim": "Capital spending flexed down fast", "evidence": "Capex cut to $360M from $410M "
         "and buybacks moderated to $20M, with net leverage at 1.4x."},
    ],
    "bear_case": [
        {"claim": "Guidance cut one quarter after a raise", "evidence": "Full-year revenue fell to "
         "$5.55-5.65B from $5.75-5.85B, and the CEO said the guide was too optimistic on volume."},
        {"claim": "FY2026 margin target withdrawn", "evidence": "Management declined to reaffirm "
         "the mid-87s operating ratio target and deferred guidance to January."},
        {"claim": "Wages run above guided yield", "evidence": "Wage inflation near 6% against yield "
         "guided to roughly 4.5% is a structural gap that volume declines make worse."},
    ],
    "catalysts": [
        {"event": "FY2026 guidance and any operating ratio target", "timing": "January"},
        {"event": "Q4 results: tonnage guided down 4% to 6%", "timing": "Q4 FY2025"},
        {"event": "San Antonio terminal opening after deferral", "timing": "Fiscal 2026"},
    ],
    "open_questions": [
        "Where does the new EPS range sit versus consensus after the cut?",
        "How does the 89.8% operating ratio compare with peers this quarter?",
        "What happens to margin if competitors keep discounting?",
    ],
    "quotes": [
        {"speaker": "Dana Whitfield, CEO", "quote": "The softness in industrial demand that we "
         "flagged in July did not stabilize. It deepened."},
        {"speaker": "Marcus Lee, CFO", "quote": "Density is exactly what a volume decline takes away."},
    ],
    "disclosure_read": "Bearish",
    "disclosure_reason": "Guidance was cut, the FY2026 target was withdrawn, and management "
    "declined to call a bottom.",
}


def memo() -> dict:
    return copy.deepcopy(MEMO)
