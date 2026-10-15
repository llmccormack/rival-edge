"""
Rival Edge — SEC EDGAR filing retrieval.

Resolves a ticker to a CIK, finds that company's recent filings of a given
form type, and downloads the primary document as text.

SEC requires every automated request to declare a contact address, and blocks
traffic that doesn't. Set SEC_USER_AGENT in .env, for example:

    SEC_USER_AGENT="Rival Edge you@example.com"

See https://www.sec.gov/os/webmaster-faq#developers
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
FILING_PAGE_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}"

SUPPORTED_FORMS = ("10-K", "10-Q")

# SEC asks for no more than 10 requests a second. One request at a time with a
# small gap keeps us far below that.
MIN_REQUEST_INTERVAL = 0.15
REQUEST_TIMEOUT = 60

_throttle_lock = threading.Lock()
_last_request = 0.0
_ticker_cache: dict[str, tuple[int, str]] = {}
_cache_lock = threading.Lock()


class EdgarError(Exception):
    """A retrieval failure worth showing to the user verbatim."""


@dataclass
class Filing:
    company: str
    cik: int
    form: str
    filed: str
    period: str
    accession: str
    document: str
    text: str = field(default="", repr=False)

    @property
    def url(self) -> str:
        return DOCUMENT_URL.format(
            cik=self.cik, accession=self.accession.replace("-", ""), document=self.document
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "form": self.form,
            "filed": self.filed,
            "period": self.period,
            "url": self.url,
            "characters": len(self.text),
            "text": self.text,
        }


def user_agent() -> str:
    agent = (os.getenv("SEC_USER_AGENT") or "").strip()
    if not agent:
        raise EdgarError(
            "SEC_USER_AGENT is not set. SEC requires a contact address on automated "
            'requests. Add SEC_USER_AGENT="Your Name you@example.com" to .env and restart.'
        )
    return agent


def _fetch(url: str) -> bytes:
    """One throttled GET against SEC, with their required headers."""
    global _last_request

    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent(), "Accept-Encoding": "identity"},
    )

    with _throttle_lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise EdgarError(
                    "SEC refused the request (403). Their fair-access policy requires "
                    "SEC_USER_AGENT to name a real contact address, so check that first. "
                    "If it already does, SEC is blocking this network: open the filing on "
                    "sec.gov and upload or paste it instead."
                ) from exc
            if exc.code == 404:
                raise EdgarError("SEC has no document at that address.") from exc
            raise EdgarError(f"SEC returned an error ({exc.code}). Try again shortly.") from exc
        except urllib.error.URLError as exc:
            raise EdgarError("Could not reach sec.gov. Check your network connection.") from exc
        finally:
            _last_request = time.monotonic()


def _fetch_json(url: str) -> Any:
    try:
        return json.loads(_fetch(url))
    except json.JSONDecodeError as exc:
        raise EdgarError("SEC returned an unreadable response. Try again shortly.") from exc


def lookup_company(ticker: str) -> tuple[int, str]:
    """Map a ticker to its CIK and registered company name."""
    symbol = ticker.strip().upper()
    if not symbol or not all(c.isalnum() or c in "-." for c in symbol):
        raise EdgarError(f"'{ticker}' is not a valid ticker symbol.")

    with _cache_lock:
        cached = _ticker_cache.get(symbol)
    if cached:
        return cached

    rows = _fetch_json(TICKERS_URL)
    entries = rows.values() if isinstance(rows, dict) else rows
    table = {
        str(row["ticker"]).upper(): (int(row["cik_str"]), str(row["title"]))
        for row in entries
        if row.get("ticker")
    }

    with _cache_lock:
        _ticker_cache.update(table)

    if symbol not in table:
        raise EdgarError(f"No SEC filer found for ticker '{symbol}'.")
    return table[symbol]


def recent_filings(ticker: str, form: str = "10-K", limit: int = 2) -> list[Filing]:
    """List a company's most recent filings of one form type, newest first."""
    if form not in SUPPORTED_FORMS:
        raise EdgarError(f"Unsupported form '{form}'. Choose one of: {', '.join(SUPPORTED_FORMS)}.")

    cik, company = lookup_company(ticker)
    submissions = _fetch_json(SUBMISSIONS_URL.format(cik=cik))
    recent = submissions.get("filings", {}).get("recent", {})

    filings: list[Filing] = []
    for index, filed_form in enumerate(recent.get("form", [])):
        if filed_form != form:
            continue
        document = recent["primaryDocument"][index]
        if not document:
            continue
        filings.append(
            Filing(
                company=submissions.get("name", company),
                cik=cik,
                form=filed_form,
                filed=recent["filingDate"][index],
                period=recent.get("reportDate", [""] * (index + 1))[index],
                accession=recent["accessionNumber"][index],
                document=document,
            )
        )
        if len(filings) >= limit:
            break

    if not filings:
        raise EdgarError(f"{company} has no recent {form} filings on EDGAR.")
    return filings


def download(filing: Filing) -> Filing:
    """Download a filing's primary document and attach its extracted text."""
    from documents import DocumentError, extract

    raw = _fetch(filing.url)
    try:
        filing.text = extract(filing.document, raw)
    except DocumentError as exc:
        raise EdgarError(f"Could not read the filing document: {exc}") from exc

    if len(filing.text) < 1000:
        raise EdgarError(
            "That filing's primary document had almost no readable text. "
            f"Open it on EDGAR and paste the text instead: {filing.url}"
        )
    return filing


def fetch(ticker: str, form: str = "10-K", index: int = 0) -> Filing:
    """Fetch one filing's text. index 0 is the most recent."""
    filings = recent_filings(ticker, form, limit=index + 1)
    if index >= len(filings):
        raise EdgarError(f"Only {len(filings)} recent {form} filing(s) found for {ticker}.")
    return download(filings[index])
