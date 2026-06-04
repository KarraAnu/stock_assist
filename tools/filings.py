"""SEC EDGAR filing metadata (no full-text extraction in v1)."""

from __future__ import annotations

import os

import requests

from .common import normalize_ticker

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# CIK lookup table is large and rarely changes — cache for the process lifetime.
_ticker_map_cache: dict[str, str] | None = None


def _user_agent() -> str:
    """SEC requires a descriptive User-Agent. Configurable via env var."""
    return os.environ.get(
        "SEC_EDGAR_USER_AGENT",
        "stock-eval-mcp/1.0 (contact: set SEC_EDGAR_USER_AGENT env var)",
    )


def _headers() -> dict:
    return {"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}


def _load_ticker_map() -> dict[str, str]:
    global _ticker_map_cache
    if _ticker_map_cache is not None:
        return _ticker_map_cache
    resp = requests.get(_TICKER_MAP_URL, headers=_headers(), timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    # Schema: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
    mapping = {}
    for entry in raw.values():
        tk = str(entry.get("ticker", "")).upper()
        if tk:
            mapping[tk] = str(entry.get("cik_str")).zfill(10)
    _ticker_map_cache = mapping
    return mapping


def get_sec_filings(ticker: str, filing_type: str = "10-Q", limit: int = 4) -> dict:
    """List recent SEC EDGAR filings of a given type for a single stock.

    Resolves the ticker to its CIK via EDGAR's company_tickers.json, then
    returns up to `limit` recent filings of `filing_type` (e.g. "10-Q", "10-K",
    "8-K"), each with filing date, report date, accession number, primary
    document name, and a direct URL to the filing index on sec.gov.

    v1 returns metadata only — it does not download or parse filing text. On any
    failure (network, unknown ticker, SEC rate-limit) it returns an
    {"error": ...} payload.

    Note: SEC requires a descriptive User-Agent. Set the SEC_EDGAR_USER_AGENT
    environment variable (e.g. "Your Name your@email.com") to avoid throttling.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    try:
        ticker_map = _load_ticker_map()
    except Exception as exc:
        hint = ""
        if "403" in str(exc):
            hint = (" — SEC rejected the User-Agent. Set the SEC_EDGAR_USER_AGENT "
                    "env var to a descriptive value with contact info, e.g. "
                    "'Your Name your@email.com'.")
        return {"error": f"could not load EDGAR ticker map: {exc}{hint}",
                "ticker": ticker}

    cik = ticker_map.get(ticker)
    if cik is None:
        return {"error": f"ticker {ticker} not found in EDGAR company_tickers.json "
                         f"(may be a foreign/OTC issuer not in the index)",
                "ticker": ticker}

    try:
        resp = requests.get(_SUBMISSIONS_URL.format(cik=cik),
                            headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"error": f"EDGAR submissions fetch failed: {exc}",
                "ticker": ticker, "cik": cik}

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cik_int = int(cik)
    wanted = filing_type.upper()
    results = []
    for i, form in enumerate(forms):
        if str(form).upper() != wanted:
            continue
        accession = accessions[i] if i < len(accessions) else None
        primary = primary_docs[i] if i < len(primary_docs) else None
        url = None
        if accession:
            no_dash = accession.replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{no_dash}"
            url = f"{base}/{primary}" if primary else f"{base}/{accession}-index.htm"
        results.append({
            "form": form,
            "filing_date": dates[i] if i < len(dates) else None,
            "report_date": report_dates[i] if i < len(report_dates) else None,
            "accession_number": accession,
            "primary_document": primary,
            "url": url,
        })
        if len(results) >= max(1, int(limit)):
            break

    return {
        "ticker": ticker,
        "cik": cik,
        "company": data.get("name"),
        "filing_type": wanted,
        "count": len(results),
        "filings": results,
    }
