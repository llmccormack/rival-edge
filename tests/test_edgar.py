"""
SEC EDGAR retrieval and HTML ingestion.

The JSON shapes below are copied from live SEC responses (company_tickers.json
and the submissions API for CIK 0000789019), so the parsing is tested against
what EDGAR actually returns. Network access itself is stubbed.
"""

from __future__ import annotations

import json

import pytest

import edgar
from documents import DocumentError, extract, extract_text_from_html

TICKERS_JSON = json.dumps({
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "3": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}).encode()

SUBMISSIONS_JSON = json.dumps({
    "cik": "0000789019",
    "name": "MICROSOFT CORP",
    "filings": {
        "recent": {
            "accessionNumber": ["0001193125-26-323660", "0001193125-26-191507", "0001193125-25-100000"],
            "filingDate": ["2026-07-29", "2026-04-29", "2025-07-30"],
            "reportDate": ["2026-06-30", "2026-03-31", "2025-06-30"],
            "form": ["10-K", "10-Q", "10-K"],
            "primaryDocument": ["msft-20260630.htm", "msft-20260331.htm", "msft-20250630.htm"],
            "primaryDocDescription": ["10-K", "10-Q", "10-K"],
        }
    },
}).encode()

FILING_HTML = b"""<html><head><style>.x{color:red}</style></head><body>
<ix:header><ix:hidden><ix:nonNumeric name="dei:EntityCentralIndexKey">0000789019</ix:nonNumeric></ix:hidden></ix:header>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
<p>Item 1A. Risk Factors</p>
<p>Our revenue was &#36;245.1&nbsp;billion, up 15% year over year.</p>
<table><tr><td>Segment</td><td>FY2026</td><td>FY2025</td></tr>
<tr><td>Productivity</td><td>$84,245</td><td>$77,728</td></tr></table>
<script>var tracking = 1;</script>
""" + b"<p>Operating expenses grew in line with revenue across each segment.</p>" * 20 + b"""
</body></html>"""


@pytest.fixture
def sec(monkeypatch):
    """Stub SEC network access and record the requested URLs."""
    calls: list[str] = []
    responses = {
        edgar.TICKERS_URL: TICKERS_JSON,
        edgar.SUBMISSIONS_URL.format(cik=789019): SUBMISSIONS_JSON,
    }

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        edgar.user_agent()  # keep the contact-address requirement in the path
        if url in responses:
            return responses[url]
        if url.endswith(".htm"):
            return FILING_HTML
        raise edgar.EdgarError("SEC has no document at that address.")

    monkeypatch.setattr(edgar, "_fetch", fake_fetch)
    monkeypatch.setenv("SEC_USER_AGENT", "Rival Edge tests@example.com")
    edgar._ticker_cache.clear()
    return calls


class TestUserAgent:
    def test_missing_contact_is_explained(self, monkeypatch):
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        with pytest.raises(edgar.EdgarError, match="SEC_USER_AGENT"):
            edgar.user_agent()

    def test_fetching_without_a_contact_fails_before_the_network(self, monkeypatch):
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        with pytest.raises(edgar.EdgarError, match="SEC_USER_AGENT"):
            edgar.fetch("MSFT")


class TestLookup:
    def test_ticker_maps_to_cik_and_name(self, sec):
        assert edgar.lookup_company("msft") == (789019, "MICROSOFT CORP")

    def test_result_is_cached(self, sec):
        edgar.lookup_company("MSFT")
        edgar.lookup_company("NVDA")
        assert sec.count(edgar.TICKERS_URL) == 1

    def test_unknown_ticker(self, sec):
        with pytest.raises(edgar.EdgarError, match="No SEC filer found"):
            edgar.lookup_company("ZZZZ")

    @pytest.mark.parametrize("bad", ["", "   ", "MS FT", "../etc"])
    def test_invalid_symbols_are_rejected(self, sec, bad):
        with pytest.raises(edgar.EdgarError, match="not a valid ticker"):
            edgar.lookup_company(bad)


class TestFilings:
    def test_recent_filings_are_filtered_by_form(self, sec):
        filings = edgar.recent_filings("MSFT", "10-K", limit=5)
        assert [f.form for f in filings] == ["10-K", "10-K"]
        assert filings[0].filed == "2026-07-29"
        assert filings[0].period == "2026-06-30"

    def test_document_url_matches_edgar_archive_layout(self, sec):
        filing = edgar.recent_filings("MSFT", "10-K", limit=1)[0]
        assert filing.url == (
            "https://www.sec.gov/Archives/edgar/data/789019/"
            "000119312526323660/msft-20260630.htm"
        )

    def test_unsupported_form(self, sec):
        with pytest.raises(edgar.EdgarError, match="Unsupported form"):
            edgar.recent_filings("MSFT", "8-K")

    def test_fetch_returns_readable_text(self, sec):
        filing = edgar.fetch("MSFT", "10-Q")
        assert filing.form == "10-Q"
        assert "Risk Factors" in filing.text
        assert "$245.1 billion" in filing.text
        assert "var tracking" not in filing.text

    def test_fetch_by_index_reaches_the_prior_filing(self, sec):
        assert edgar.fetch("MSFT", "10-K", index=1).filed == "2025-07-30"

    def test_fetch_past_the_end(self, sec):
        with pytest.raises(edgar.EdgarError, match="Only 2 recent"):
            edgar.fetch("MSFT", "10-K", index=5)

    def test_as_dict_carries_provenance(self, sec):
        payload = edgar.fetch("MSFT").as_dict()
        assert payload["company"] == "MICROSOFT CORP"
        assert payload["characters"] == len(payload["text"])
        assert payload["url"].startswith("https://www.sec.gov/Archives/")


class TestHtmlExtraction:
    def test_inline_xbrl_tagging_is_dropped(self):
        text = extract_text_from_html(FILING_HTML.decode())
        assert "0000789019" not in text, "ix:hidden XBRL data leaked into the text"
        assert "color:red" not in text
        assert "var tracking" not in text

    def test_entities_and_nbsp_become_plain_text(self):
        assert "$245.1 billion" in extract_text_from_html(FILING_HTML.decode())

    def test_table_cells_stay_separated(self):
        text = extract_text_from_html(FILING_HTML.decode())
        assert "Productivity | $84,245 | $77,728" in text

    def test_block_elements_become_line_breaks(self):
        text = extract_text_from_html("<p>First line.</p><p>Second line.</p>")
        assert text.splitlines() == ["First line.", "Second line."]


class TestExtractDispatch:
    def test_html_extension(self):
        assert "Revenue up." in extract("10k.html", b"<html><body><p>Revenue up.</p></body></html>")

    def test_txt_is_normalized(self):
        assert extract("call.txt", b"Revenue   was\t up.\r\n\r\n\r\n\r\nNext.") == "Revenue was up.\n\nNext."

    def test_unsupported_extension(self):
        with pytest.raises(DocumentError, match="Unsupported file type"):
            extract("model.xlsx", b"x")
