"""
Rival Edge — Flask application.

Routes:
    GET  /                       single-page UI
    POST /api/analyze            analyze one document     -> NDJSON progress stream
    POST /api/compare            compare two periods      -> NDJSON progress stream
    POST /api/export             render results as a PDF  -> application/pdf
    GET  /api/samples/<name>     bundled sample transcript
    GET  /api/examples/<kind>    saved output from a real run, viewable without a key
"""

from __future__ import annotations

import io
import json
import os
import queue
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from analyzer import (
    MODEL_LABEL,
    AnalyzerError,
    Progress,
    analyze_document,
    compare_documents,
    extract_text_from_pdf,
)
from report import build_analysis_pdf, build_comparison_pdf, slugify

load_dotenv()

ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = ROOT / "samples"

SAMPLE_TRANSCRIPTS = {
    "q2": "northwind-q2-fy2025-earnings-call.txt",
    "q3": "northwind-q3-fy2025-earnings-call.txt",
}
EXAMPLE_OUTPUTS = {
    "analysis": "example-analysis.json",
    "comparison": "example-comparison.json",
}

ALLOWED_EXTENSIONS = {".txt", ".pdf"}

REPO_URL = "https://github.com/llmccormack/rival-edge"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload ceiling


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------


def read_document(text_field: str, file_field: str) -> str:
    """Pull document text from an uploaded file if present, otherwise the pasted text."""
    upload = request.files.get(file_field)

    if upload and upload.filename:
        extension = os.path.splitext(upload.filename)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise AnalyzerError(f"Unsupported file type '{extension}'. Upload a .txt or .pdf.")
        if extension == ".pdf":
            return extract_text_from_pdf(io.BytesIO(upload.read()))
        return upload.read().decode("utf-8", errors="replace")

    pasted = (request.form.get(text_field) or "").strip()
    if pasted:
        return pasted

    raise AnalyzerError("No document provided. Paste text or upload a .txt or .pdf file.")


def examples_available() -> bool:
    return all((SAMPLES_DIR / name).is_file() for name in EXAMPLE_OUTPUTS.values())


# --------------------------------------------------------------------------
# Progress streaming
#
# Analysis takes anywhere from a few seconds to several minutes, so the API
# streams newline-delimited JSON: progress events as each stage actually
# starts, then a single result or error event.
# --------------------------------------------------------------------------


def stream_job(job: Callable[[Progress], dict[str, Any]]) -> Response:
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def worker() -> None:
        try:
            result = job(lambda message: events.put({"type": "progress", "message": message}))
            events.put({"type": "result", "data": result})
        except AnalyzerError as exc:
            events.put({"type": "error", "error": str(exc)})
        except Exception:
            app.logger.exception("Analysis failed")
            events.put({"type": "error", "error": "Unexpected server error. Check the console."})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate() -> Iterator[str]:
        while (event := events.get()) is not None:
            yield json.dumps(event) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template(
        "index.html",
        demo=False,
        repo_url=REPO_URL,
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
        examples_available=examples_available(),
        model_label=MODEL_LABEL,
        year=datetime.now().year,
    )


@app.post("/api/analyze")
def api_analyze():
    try:
        document = read_document("text", "file")
    except AnalyzerError as exc:
        return jsonify({"error": str(exc)}), 400
    return stream_job(lambda progress: analyze_document(document, progress))


@app.post("/api/compare")
def api_compare():
    try:
        earlier = read_document("text_a", "file_a")
        later = read_document("text_b", "file_b")
    except AnalyzerError as exc:
        return jsonify({"error": str(exc)}), 400
    return stream_job(lambda progress: compare_documents(earlier, later, progress))


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


@app.get("/api/samples/<name>")
def api_sample(name: str):
    filename = SAMPLE_TRANSCRIPTS.get(name)
    if filename is None or not (SAMPLES_DIR / filename).is_file():
        abort(404)
    return Response((SAMPLES_DIR / filename).read_text(encoding="utf-8"), mimetype="text/plain")


@app.get("/api/examples/<kind>")
def api_example(kind: str):
    filename = EXAMPLE_OUTPUTS.get(kind)
    if filename is None or not (SAMPLES_DIR / filename).is_file():
        abort(404)
    return app.response_class((SAMPLES_DIR / filename).read_text(encoding="utf-8"), mimetype="application/json")


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "That file is larger than the 25 MB limit."}), 413


@app.errorhandler(500)
def server_error(_):
    return jsonify({"error": "Something went wrong on the server. Check the console."}), 500


if __name__ == "__main__":
    # 5001 rather than Flask's usual 5000: on macOS the AirPlay Receiver holds
    # port 5000, which makes the server look broken for no good reason.
    port = int(os.getenv("PORT", "5001"))

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n  Warning: ANTHROPIC_API_KEY is not set. Live analysis is disabled.")
        print("  Copy .env.example to .env and add your key, then restart.\n")

    print(f"\n  Rival Edge running at http://127.0.0.1:{port}\n")

    try:
        # Debug mode (auto-reload, interactive tracebacks) is opt-in via FLASK_DEBUG=1.
        app.run(host="127.0.0.1", port=port, debug=os.getenv("FLASK_DEBUG") == "1", threaded=True)
    except OSError as exc:
        print(f"\n  Could not bind to port {port}: {exc}")
        print("  Something else is using it. Try:  PORT=5002 python app.py\n")
        raise SystemExit(1) from exc
