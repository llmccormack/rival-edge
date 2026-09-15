"""The static GitHub Pages demo build."""

from __future__ import annotations

import json

import build_static_demo


def test_build_produces_a_self_contained_site(tmp_path):
    site = build_static_demo.build(tmp_path / "site")

    html = (site / "index.html").read_text(encoding="utf-8")
    assert 'data-demo="true"' in html
    assert "You're viewing a static demo" in html
    assert 'href="static/style.css"' in html
    assert 'src="static/app.js"' in html
    assert '"/static/' not in html, "root-relative paths break under /<repo>/ on Pages"

    assert (site / "static" / "app.js").is_file()
    assert (site / ".nojekyll").is_file()
    for name in ("example-analysis.json", "example-comparison.json"):
        json.loads((site / "samples" / name).read_text(encoding="utf-8"))
    assert (site / "samples" / "northwind-q3-fy2025-earnings-call.txt").is_file()
    assert (site / "docs" / "example-comparison.pdf").read_bytes()[:4] == b"%PDF"


def test_server_mode_is_not_demo(client):
    assert b'data-demo="false"' in client.get("/").data
