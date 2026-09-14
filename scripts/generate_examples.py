"""
Regenerate the saved example output from the bundled sample transcripts.

Runs the real analysis pipeline against the Claude API, so it needs
ANTHROPIC_API_KEY. Writes:

    samples/example-analysis.json     single-document analysis of the Q3 call
    samples/example-comparison.json   Q2 -> Q3 comparison
    docs/example-analysis.pdf         PDF export of the analysis
    docs/example-comparison.pdf       PDF export of the comparison

The app serves the JSON files as a browsable example when no API key is set.

Usage:
    python scripts/generate_examples.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from analyzer import AnalyzerError, analyze_document, compare_documents  # noqa: E402
from report import build_analysis_pdf, build_comparison_pdf  # noqa: E402

SAMPLES = ROOT / "samples"
DOCS = ROOT / "docs"


def log(message: str) -> None:
    print(f"  · {message}", flush=True)


def summarize(usage: dict) -> str:
    return (
        f"{usage['calls']} calls, {usage['input_tokens']:,} tokens in, "
        f"{usage['output_tokens']:,} out, {usage['elapsed_seconds']}s"
    )


def main() -> int:
    earlier = (SAMPLES / "northwind-q2-fy2025-earnings-call.txt").read_text(encoding="utf-8")
    later = (SAMPLES / "northwind-q3-fy2025-earnings-call.txt").read_text(encoding="utf-8")
    generated = date.today().strftime("%B %-d, %Y")
    DOCS.mkdir(exist_ok=True)

    try:
        print("\nSingle-document analysis (Q3 FY2025)")
        analysis = analyze_document(later, log)
        analysis["_meta"]["generated_at"] = generated
        print(f"  done: {summarize(analysis['_meta']['usage'])}")

        print("\nComparison (Q2 -> Q3 FY2025)")
        comparison = compare_documents(earlier, later, log)
        comparison["_meta"]["generated_at"] = generated
        print(f"  done: {summarize(comparison['_meta']['usage'])}")
    except AnalyzerError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    (SAMPLES / "example-analysis.json").write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (SAMPLES / "example-comparison.json").write_text(json.dumps(comparison, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (DOCS / "example-analysis.pdf").write_bytes(build_analysis_pdf(analysis))
    (DOCS / "example-comparison.pdf").write_bytes(build_comparison_pdf(comparison))

    print("\nWrote samples/example-*.json and docs/example-*.pdf\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
