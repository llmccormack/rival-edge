"""
Shared test setup.

No test ever reaches the real Claude API. Every test runs with the API key
cleared and ANTHROPIC_BASE_URL pointed at a closed local port, so an
accidental live call fails fast instead of spending money. Tests that exercise
the Claude integration use the `claude_stub` fixture: a local HTTP server that
speaks the Messages streaming protocol.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Set before app.py runs load_dotenv(), which never overrides existing
# variables, so a developer's real .env key can't leak into the suite.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"

import analyzer  # noqa: E402
import fixtures  # noqa: E402
from app import app as flask_app  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("RIVAL_EDGE_FALLBACKS", raising=False)
    analyzer._client = None
    yield
    analyzer._client = None


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


# --------------------------------------------------------------------------
# Claude API stub
# --------------------------------------------------------------------------


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


@dataclass
class ClaudeStub:
    """Controls and records traffic for the local Messages API stub."""

    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    status: int = 200
    delay: float = 0.0
    analysis: dict[str, Any] = field(default_factory=fixtures.analysis)
    comparison: dict[str, Any] = field(default_factory=lambda: fixtures.comparison_payload()["comparison"])
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reply_for(self, body: dict[str, Any]) -> str:
        system = body.get("system", "")
        if "research associate" in system:
            section = body["messages"][0]["content"].split("\n", 1)[0]
            return f"Notes for {section}"
        if "comparison note" in system:
            return json.dumps(self.comparison)
        return json.dumps(self.analysis)


@pytest.fixture
def claude_stub(monkeypatch):
    stub: ClaudeStub

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            record = {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
                "started": time.monotonic(),
            }
            with stub.lock:
                stub.requests.append(record)

            if stub.status != 200:
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid x-api-key"},
                }).encode())
                return

            time.sleep(stub.delay)
            text = "" if stub.stop_reason == "refusal" else stub.reply_for(body)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            out = self.wfile
            out.write(_sse("message_start", {"type": "message_start", "message": {
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": body["model"], "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1},
            }}))
            out.write(_sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""},
            }))
            for start in range(0, len(text), 300):
                out.write(_sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text[start:start + 300]},
                }))
            out.write(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
            out.write(_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stub.stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 450},
            }))
            out.write(_sse("message_stop", {"type": "message_stop"}))
            out.flush()
            record["finished"] = time.monotonic()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub = ClaudeStub(url=f"http://127.0.0.1:{server.server_address[1]}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-stub")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", stub.url)
    analyzer._client = None

    yield stub

    server.shutdown()
    server.server_close()


@pytest.fixture
def long_document_limits(monkeypatch):
    """Shrink chunking thresholds so the map/reduce path runs on a small input."""
    monkeypatch.setattr(analyzer, "SINGLE_PASS_CHAR_LIMIT", 3_000)
    monkeypatch.setattr(analyzer, "CHUNK_CHARS", 1_500)
    monkeypatch.setattr(analyzer, "CHUNK_OVERLAP", 100)
