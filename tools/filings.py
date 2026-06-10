"""SEC EDGAR: filing metadata + XBRL footnote metrics (RPO/backlog)."""

from __future__ import annotations

import os
from datetime import date

import requests

from .common import TTL_FMP_STATEMENTS, cached, normalize_ticker, num

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/"
                "us-gaap/{tag}.json")
_RPO_TAG = "RevenueRemainingPerformanceObligation"

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


def get_rpo(ticker: str) -> dict:
    """Remaining performance obligations (contracted backlog) from SEC XBRL.

    RPO is contracted-but-unrecognized revenue — the closest thing to an
    official "backlog" number, and often THE decisive commercial metric for
    early-stage and contract-driven names (it is where IonQ's $470M showed up
    while quarterly revenue was still $65M). It is a footnote disclosure, so
    FMP's normalized statements cannot supply it; EDGAR's companyconcept API
    serves the XBRL-tagged total for free.

    Returns the latest reported RPO, the year-ago value and YoY growth, and
    the recent series (USD millions). Coverage gaps are normal — some filers
    tag only expected-timing buckets, not the total — in which case this
    returns {"available": false} with a note, not an error.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    try:
        ticker_map = _load_ticker_map()
    except Exception as exc:
        return {"error": f"could not load EDGAR ticker map: {exc}", "ticker": ticker}
    cik = ticker_map.get(ticker)
    if cik is None:
        return {"ticker": ticker, "available": False,
                "note": "ticker not in EDGAR index (foreign/OTC issuer?)"}

    def produce():
        resp = requests.get(_CONCEPT_URL.format(cik=cik, tag=_RPO_TAG),
                            headers=_headers(), timeout=15)
        if resp.status_code == 404:
            return {"__not_tagged__": True}
        resp.raise_for_status()
        return resp.json()

    try:
        data = cached(("edgar_concept", cik, _RPO_TAG), produce,
                      ttl=TTL_FMP_STATEMENTS, disk=True)
    except Exception as exc:
        return {"error": f"EDGAR companyconcept fetch failed: {exc}",
                "ticker": ticker}

    if data.get("__not_tagged__"):
        return {"ticker": ticker, "available": False,
                "note": "filer does not XBRL-tag total RPO (some tag only "
                        "timing buckets) — check the 10-Q/10-K text directly"}

    # Dedup repeated disclosures of the same period-end, keeping the most
    # recently filed value (amended/restated figures supersede).
    by_end: dict[str, dict] = {}
    for u in (data.get("units") or {}).get("USD") or []:
        end, val = u.get("end"), num(u.get("val"))
        if not end or val is None:
            continue
        if end not in by_end or str(u.get("filed", "")) > str(by_end[end].get("filed", "")):
            by_end[end] = u
    ends = sorted(by_end)
    if not ends:
        return {"ticker": ticker, "available": False,
                "note": "RPO tag present but no usable USD values"}

    latest_end = ends[-1]
    latest_val = num(by_end[latest_end]["val"])

    # Year-ago comparison: the period-end closest to 365 days prior (±120d).
    yoy_growth = None
    try:
        target = date.fromisoformat(latest_end).toordinal() - 365
        best, best_dist = None, 121
        for e in ends[:-1]:
            dist = abs(date.fromisoformat(e).toordinal() - target)
            if dist < best_dist:
                best, best_dist = e, dist
        if best:
            prior = num(by_end[best]["val"])
            if prior and prior > 0:
                yoy_growth = round((latest_val / prior - 1) * 100, 1)
    except ValueError:
        pass

    return {
        "ticker": ticker,
        "available": True,
        "latest": {"end": latest_end, "value": latest_val,
                   "usd_m": round(latest_val / 1e6, 1)},
        "yoy_growth_pct": yoy_growth,
        "series_usd_m": {e: round(num(by_end[e]["val"]) / 1e6, 1)
                         for e in ends[-8:]},
        "source": f"SEC EDGAR XBRL (us-gaap:{_RPO_TAG})",
        "note": "total RPO includes deferred revenue + unbilled contracted "
                "backlog; recognition timing varies — see the filing for the "
                "expected-timing split",
    }


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
