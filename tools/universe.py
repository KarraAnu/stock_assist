"""Stock universes for the screener / autopilot.

S&P 500 constituents are fetched from a stable public CSV and cached to disk,
with an embedded fallback so the autopilot never breaks on a network hiccup.
"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import requests

from .common import data_dir

_CSV_URL = ("https://raw.githubusercontent.com/datasets/"
            "s-and-p-500-companies/main/data/constituents.csv")
_CACHE_TTL = 7 * 24 * 3600  # refresh weekly

# Fallback: a liquid large-cap core across sectors (used only if the fetch fails).
_FALLBACK = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "ORCL", "CRM", "ADBE",
    "AMD", "INTC", "QCOM", "TXN", "CSCO", "IBM", "NOW", "INTU", "AMAT", "MU",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP", "V", "MA",
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "BMY", "TMO", "ABT", "DHR", "AMGN",
    "GILD", "CVS", "CI", "ELV", "MDT", "BDX", "MCK", "GEHC",
    "XOM", "CVX", "COP", "SLB", "OXY", "PSX", "VLO", "MPC", "KMI", "WMB",
    "PG", "KO", "PEP", "COST", "WMT", "MDLZ", "CL", "KMB", "MO", "TGT",
    "HD", "LOW", "MCD", "SBUX", "NKE", "CMG", "BKNG", "DIS", "NFLX",
    "BA", "CAT", "GE", "HON", "RTX", "LMT", "NOC", "GD", "DE", "UPS", "FDX", "MMM",
    "T", "VZ", "TMUS", "CMCSA", "TSLA", "F", "GM",
    "PLD", "AMT", "EQIX", "SPG", "O",
]


def sp500_tickers() -> list[str]:
    """Current S&P 500 tickers (cached weekly to disk; embedded fallback)."""
    cache = data_dir() / "sp500.csv"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < _CACHE_TTL:
        return _parse(cache.read_text(encoding="utf-8"))
    try:
        resp = requests.get(_CSV_URL, timeout=20)
        resp.raise_for_status()
        cache.write_text(resp.text, encoding="utf-8")
        tickers = _parse(resp.text)
        if len(tickers) >= 400:
            return tickers
    except Exception:
        pass
    if cache.exists():
        try:
            return _parse(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    return list(_FALLBACK)


def _parse(text: str) -> list[str]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
        if sym:
            out.append(sym.replace(".", "-"))  # BRK.B -> BRK-B for yfinance
    return out


# Sleeve B — growth / thematic / speculative universe (curated, expandable).
_GROWTH = [
    # Quantum
    "IONQ", "RGTI", "QBTS", "QUBT",
    # Nuclear / SMR / uranium
    "OKLO", "SMR", "NNE", "LEU", "UEC", "BWXT", "LTBR",
    # Drones / eVTOL / space
    "AVAV", "KTOS", "RCAT", "ONDS", "UMAC", "ACHR", "JOBY", "RKLB", "ASTS", "LUNR",
    # Growth tech / software / AI
    "PLTR", "SNOW", "NET", "DDOG", "CRWD", "PANW", "U", "PATH", "AI", "SOUN",
    "APP", "SHOP", "RBLX", "DASH", "ABNB", "UBER",
    # Fintech / crypto-adjacent
    "SOFI", "COIN", "HOOD", "AFRM", "UPST", "NU",
    # Consumer / health growth
    "RDDT", "HIMS", "DKNG", "CELH", "CAVA",
    # Biotech / EV
    "MRNA", "RIVN", "LCID", "CHPT",
]


# Market-cap bands ($) to page through — the screener caps each call at ~100
# rows, so banding captures mid-caps, not just the mega-cap top 100.
_CAP_BANDS = [(2e9, 10e9), (10e9, 50e9), (50e9, 300e9), (300e9, None)]


def _fmp_growth_screen() -> list[str]:
    """Dynamic pull of higher-beta (growth-leaning) US names across the full
    market-cap spectrum from FMP's screener. The growth SCORING step does the
    actual growth selection; this just builds a broad candidate pool so we're
    not limited to a hand-list.
    """
    from .fmp import FMPError, fmp_get, have_key
    if not have_key():
        return []
    out, seen = [], set()
    for lo, hi in _CAP_BANDS:
        params = {"marketCapMoreThan": int(lo), "betaMoreThan": 1.1,
                  "isEtf": "false", "isActivelyTrading": "true", "limit": 100}
        if hi:
            params["marketCapLowerThan"] = int(hi)
        try:
            rows = fmp_get("company-screener", **params)
        except FMPError:
            continue
        for r in rows if isinstance(rows, list) else []:
            sym = str(r.get("symbol") or "").upper().strip()
            exch = str(r.get("exchange") or "").upper()
            is_fund = len(sym) == 5 and sym.endswith("X")   # mutual-fund convention
            us_exch = any(x in exch for x in
                          ("NASDAQ", "NYSE", "NEW YORK", "AMEX", "AMERICAN"))
            if (sym and sym not in seen and sym.isalpha() and len(sym) <= 5
                    and not is_fund and us_exch):
                seen.add(sym)
                out.append(sym)
    return out


def growth_universe() -> list[str]:
    """Curated thematic names (small speculative ones a market-cap screen misses)
    UNION a dynamic screener pull of broad growth-leaning mid/large caps.
    """
    seen, out = set(), []
    for t in list(_GROWTH) + _fmp_growth_screen():
        u = t.upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def ipo_universe(months_back: int = 9, limit: int = 80) -> list[str]:
    """Recently-IPO'd US tickers from FMP's IPO calendar (already trading)."""
    from datetime import date, timedelta
    from .fmp import FMPError, fmp_get, have_key
    if not have_key():
        return []
    today = date.today()
    start = (today - timedelta(days=int(months_back) * 31)).isoformat()
    try:
        rows = fmp_get("ipos-calendar", **{"from": start, "to": today.isoformat()})
    except FMPError:
        return []
    us = {"NASDAQ", "NYSE", "AMEX", "NASDAQ Global Select", "New York Stock Exchange",
          "NasdaqGS", "NasdaqGM", "NasdaqCM", "NYSE American"}
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        sym = str(r.get("symbol") or "").upper().strip()
        exch = str(r.get("exchange") or "").upper()
        d = str(r.get("date") or "")
        # Drop SPAC units/warrants/rights/preferreds (5-char tickers ending in
        # U/W/R/P) — they dominate the raw IPO calendar and aren't investable
        # common stock.
        is_unit = len(sym) == 5 and sym[-1] in ("U", "W", "R", "P")
        us_exch = any(x in exch for x in ("NASDAQ", "NYSE", "NEW YORK", "AMEX", "AMERICAN"))
        if (sym and sym not in seen and d and d <= today.isoformat()
                and us_exch and sym.isalpha() and len(sym) <= 5 and not is_unit):
            seen.add(sym)
            out.append(sym)
        if len(out) >= limit:
            break
    return out


UNIVERSES = {"sp500": sp500_tickers, "growth": growth_universe, "ipo": ipo_universe}


def get_universe(name: str = "sp500") -> list[str]:
    fn = UNIVERSES.get(name)
    return fn() if fn else list(_FALLBACK)
