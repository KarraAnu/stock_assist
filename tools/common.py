"""Shared infrastructure: data paths, TTL cache, yfinance wrappers, validation.

The design contract for every data tool that lives on top of this module:
  - Network fetches go through the cached wrappers here, so one evaluation that
    calls five tools for the same ticker hits Yahoo once, not five times.
  - Missing/empty data fails LOUDLY. yfinance silently returns None or empty
    frames when rate-limited; a half-empty dict that *looks* complete is the
    dangerous failure for a tool whose job is killing hallucinated numbers.
    Use `missing_data_error` to return {"error": ..., "partial": {...}}.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import yfinance as yf

# --------------------------------------------------------------------------
# Data directory (absolute, never CWD — MCP servers don't launch from here)
# --------------------------------------------------------------------------

def data_dir() -> Path:
    """Resolve the runtime data directory.

    Order: STOCK_MCP_DATA_DIR env var, else <project>/data anchored to this
    file's location. Created on first access. Never relative to CWD, because
    Claude Desktop launches the server from an unpredictable working directory.
    """
    env = os.environ.get("STOCK_MCP_DATA_DIR")
    base = Path(env) if env else (Path(__file__).resolve().parent.parent / "data")
    base.mkdir(parents=True, exist_ok=True)
    return base


# --------------------------------------------------------------------------
# Tiered TTL cache: in-memory always, optional disk layer for JSON payloads.
#
# TTLs match how fast each source actually changes: quotes drift intraday,
# price history matters at day granularity, FMP financial statements change
# quarterly. A sector run touches the same ticker across several phases
# minutes apart — the old flat 60s TTL meant every phase refetched everything.
# The disk layer (data/cache/) makes FMP/info reads survive server restarts
# and repeat runs nearly free.
# --------------------------------------------------------------------------

TTL_QUOTE = 15 * 60        # yfinance .info / .fast_info
TTL_HISTORY = 60 * 60      # daily price history
TTL_NEWS = 30 * 60
TTL_FMP_STATEMENTS = 24 * 3600   # statements / ratios / profile / peers / scores
TTL_FMP_VOLATILE = 60 * 60       # screener, calendars, insider feed

# Failed/empty fetches are remembered only briefly: long enough to damp a
# retry storm, short enough that a transient rate-limit isn't pinned for the
# full TTL (dogfooding: GEV failed once and every retry for 15 min failed
# from cache while the source had already recovered).
TTL_NEGATIVE = 60.0

_cache: dict[tuple, tuple[float, Any, bool]] = {}


def _disk_path(key: tuple) -> Path:
    digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def cached(key: tuple, producer: Callable[[], Any], ttl: float = TTL_QUOTE,
           disk: bool = False, validate: Callable[[Any], bool] | None = None) -> Any:
    """Memory-first TTL cache; `disk=True` adds a JSON file layer.

    `validate` decides whether a produced value is a real result: invalid
    (empty/failed) values get only TTL_NEGATIVE so a transient provider
    outage isn't remembered for the full TTL. Disk is only for JSON-
    serializable, VALID values. Wall-clock timestamps so disk entries age
    correctly across restarts.
    """
    now = time.time()
    hit = _cache.get(key)
    if hit is not None:
        ts, val, ok = hit
        if (now - ts) < (ttl if ok else TTL_NEGATIVE):
            return val

    if disk:
        path = _disk_path(key)
        try:
            if path.exists():
                blob = json.loads(path.read_text(encoding="utf-8"))
                if (now - blob["t"]) < ttl:
                    _cache[key] = (blob["t"], blob["v"], True)
                    return blob["v"]
        except Exception:
            pass  # corrupt/unreadable cache file — fall through to refetch

    value = producer()
    ok = bool(validate(value)) if validate is not None else True
    _cache[key] = (now, value, ok)
    if disk and ok:
        try:
            _disk_path(key).write_text(json.dumps({"t": now, "v": value}),
                                       encoding="utf-8")
        except (TypeError, ValueError, OSError):
            pass  # not serializable / disk issue — memory cache still holds it
    return value


# Backward-compatible alias (old call sites assume the default TTL).
def _cached(key: tuple, producer: Callable[[], Any]) -> Any:
    return cached(key, producer)


def clear_cache(include_disk: bool = False) -> None:
    _cache.clear()
    if include_disk:
        cache_dir = data_dir() / "cache"
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass


def parallel_map(fn: Callable[[Any], Any], items: list, workers: int = 8) -> list:
    """Run `fn` over `items` concurrently, preserving order.

    For I/O-bound fan-out (yfinance/FMP fetches). A failure in one item must
    not kill a whole batch, so exceptions are returned as {"error": ...} dicts —
    consistent with the loud-failure contract every tool already uses.
    """
    if not items:
        return []

    def safe(item):
        try:
            return fn(item)
        except Exception as exc:  # noqa: BLE001 — batch isolation by design
            return {"error": f"{type(exc).__name__}: {exc}", "item": str(item)}

    if len(items) == 1:
        return [safe(items[0])]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(safe, items))


# --------------------------------------------------------------------------
# yfinance fetch wrappers (cached)
# --------------------------------------------------------------------------

def get_info(ticker: str) -> dict:
    """yfinance .info dict (rich but slow/fragile). Returns {} on failure."""
    def produce() -> dict:
        try:
            info = yf.Ticker(ticker).info or {}
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}
    return cached(("info", ticker.upper()), produce, ttl=TTL_QUOTE, disk=True,
                  validate=lambda v: bool(v))


def get_fast_info(ticker: str) -> dict:
    """yfinance .fast_info (cheap, different code path than .info).

    Used to cross-check price against .info — disagreement is a data-quality
    signal worth surfacing rather than silently trusting one source.
    """
    def produce() -> dict:
        try:
            fi = yf.Ticker(ticker).fast_info
            # fast_info is a lazy mapping-like object; materialize what we need.
            out: dict = {}
            for k in ("last_price", "market_cap", "shares", "currency",
                      "year_high", "year_low", "fifty_day_average",
                      "two_hundred_day_average"):
                try:
                    out[k] = fi[k]
                except Exception:
                    out[k] = None
            return out
        except Exception:
            return {}
    return cached(("fast_info", ticker.upper()), produce, ttl=TTL_QUOTE,
                  validate=lambda v: bool(v) and any(x is not None
                                                     for x in v.values()))


_PERIOD_DAYS = {"1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731,
                "3y": 1096, "5y": 1827, "10y": 3653, "max": 36500}


def _fmp_history_df(ticker: str, period: str):
    """Daily closes from FMP (paid, SLA-backed). None when unavailable.

    Tries the dividend-adjusted series first so return math matches
    yfinance's auto_adjust; falls back to the split-adjusted close.
    """
    from .fmp import FMPError, fmp_get, have_key
    if not have_key():
        return None
    rows = None
    for path in ("historical-price-eod/dividend-adjusted",
                 "historical-price-eod/full"):
        try:
            rows = fmp_get(path, symbol=ticker)
            if isinstance(rows, list) and rows:
                break
        except FMPError:
            rows = None
    if not rows:
        return None

    import pandas as pd
    from datetime import date, timedelta
    cutoff = (date.today()
              - timedelta(days=_PERIOD_DAYS.get(period, 731))).isoformat()
    recs = [(r.get("date"),
             r.get("adjClose") if r.get("adjClose") is not None else r.get("close"),
             r.get("volume"))
            for r in rows
            if r.get("date") and r.get("date") >= cutoff
            and (r.get("adjClose") is not None or r.get("close") is not None)]
    if not recs:
        return None
    recs.sort()  # FMP returns newest-first; consumers expect ascending
    df = pd.DataFrame(recs, columns=["date", "Close", "Volume"])
    df.index = pd.to_datetime(df.pop("date"))
    return df


def get_history(ticker: str, period: str = "2y", interval: str = "1d"):
    """Daily price history DataFrame (Close, Volume; ascending dates).

    FMP is the PRIMARY source for daily data — yfinance's history endpoint is
    the most rate-limited path in the stack and repeatedly blanked the
    risk/momentum layer in live runs. yfinance remains the fallback (and the
    only path for intraday intervals). Returns an empty DataFrame on failure.
    """
    def produce():
        if interval == "1d":
            df = _fmp_history_df(ticker, period)
            if df is not None and not df.empty:
                return df
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval,
                                           auto_adjust=True)
            if df is not None and not df.empty:
                # tz-naive index so FMP- and yfinance-sourced series align
                # (the SPY benchmark join in risk_metrics needs this).
                df.index = df.index.tz_localize(None)
            return df
        except Exception:
            import pandas as pd
            return pd.DataFrame()
    # DataFrames aren't JSON-serializable — memory cache only.
    return cached(("history", ticker.upper(), period, interval), produce,
                  ttl=TTL_HISTORY,
                  validate=lambda df: df is not None and not df.empty)


def get_dividends_raw(ticker: str):
    """yfinance dividend payment series. None on failure (an EMPTY series is a
    valid result — non-payers — and is cached normally)."""
    def produce():
        try:
            return yf.Ticker(ticker).dividends
        except Exception:
            return None
    return cached(("dividends", ticker.upper()), produce, ttl=TTL_HISTORY,
                  validate=lambda v: v is not None)


def get_earnings_dates_raw(ticker: str):
    """yfinance earnings-dates DataFrame (slow endpoint). None on failure."""
    def produce():
        try:
            return yf.Ticker(ticker).earnings_dates
        except Exception:
            return None
    return cached(("earnings_dates", ticker.upper()), produce, ttl=TTL_HISTORY,
                  validate=lambda v: v is not None)


def get_news_raw(ticker: str) -> list:
    """Raw yfinance .news list. Returns [] on failure (schema is unstable)."""
    def produce() -> list:
        try:
            news = yf.Ticker(ticker).news
            return news if isinstance(news, list) else []
        except Exception:
            return []
    return cached(("news", ticker.upper()), produce, ttl=TTL_NEWS,
                  validate=lambda v: bool(v))


# --------------------------------------------------------------------------
# Value coercion + validation
# --------------------------------------------------------------------------

def num(value: Any) -> float | int | None:
    """Coerce to a JSON-safe number. NaN/inf/None -> None.

    yfinance and pandas hand back NaN for missing values; NaN is not
    JSON-serializable and must not be confused with a real zero.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return int(value)
    return f


def normalize_ticker(ticker: str) -> str:
    return (ticker or "").strip().upper()


def missing_data_error(ticker: str, missing: list[str], partial: dict) -> dict:
    """Standard loud-failure payload when critical fields are absent.

    Returns a dict that is unambiguously an error — Claude should treat this as
    "data unavailable", not reason around a quietly-empty result.
    """
    return {
        "error": f"data unavailable for {ticker}: missing {', '.join(missing)} "
                 f"(yfinance returned empty/None — likely rate-limited or "
                 f"delisted/invalid ticker)",
        "ticker": ticker,
        "missing_fields": missing,
        "partial": partial,
    }
