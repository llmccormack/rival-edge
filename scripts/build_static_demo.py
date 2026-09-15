"""
Build the static demo published to GitHub Pages.

Renders the real template in demo mode and copies the assets, sample
transcripts, saved examples, and example PDFs alongside it. The result runs
without a server or API key: it shows saved output from real Claude runs.

Usage:
    python scripts/build_static_demo.py [output_dir]    (default: site/)
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import render_template  # noqa: E402

from analyzer import MODEL_LABEL  # noqa: E402
from app import EXAMPLE_OUTPUTS, REPO_URL, SAMPLE_TRANSCRIPTS, SAMPLES_DIR, app  # noqa: E402

EXAMPLE_PDFS = ["example-analysis.pdf", "example-comparison.pdf"]


def build(output: Path) -> Path:
    for name in [*SAMPLE_TRANSCRIPTS.values(), *EXAMPLE_OUTPUTS.values()]:
        if not (SAMPLES_DIR / name).is_file():
            raise SystemExit(f"Missing samples/{name}. Run scripts/generate_examples.py first.")

    if output.exists():
        shutil.rmtree(output)
    (output / "samples").mkdir(parents=True)
    (output / "docs").mkdir()

    with app.test_request_context("/"):
        html = render_template(
            "index.html",
            demo=True,
            repo_url=REPO_URL,
            has_api_key=False,
            examples_available=True,
            model_label=MODEL_LABEL,
            year=datetime.now().year,
        )

    # Pages serves the site from /<repo>/, so root-relative asset paths would break.
    html = html.replace('href="/static/', 'href="static/').replace('src="/static/', 'src="static/')
    (output / "index.html").write_text(html, encoding="utf-8")

    shutil.copytree(ROOT / "static", output / "static")
    for name in [*SAMPLE_TRANSCRIPTS.values(), *EXAMPLE_OUTPUTS.values()]:
        shutil.copy2(SAMPLES_DIR / name, output / "samples" / name)
    for name in EXAMPLE_PDFS:
        shutil.copy2(ROOT / "docs" / name, output / "docs" / name)

    # Serve files as-is; no Jekyll processing.
    (output / ".nojekyll").touch()
    return output


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site"
    print(f"Built static demo in {build(target)}")
