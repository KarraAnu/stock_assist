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

_cache: dict[tuple, tuple[float, Any]] = {}


def _disk_path(key: tuple) -> Path:
    digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def cached(key: tuple, producer: Callable[[], Any], ttl: float = TTL_QUOTE,
           disk: bool = False) -> Any:
    """Memory-first TTL cache; `disk=True` adds a JSON file layer.

    Disk is only for JSON-serializable values (FMP payloads, info dicts) —
    non-serializable values silently stay memory-only. Wall-clock timestamps so
    disk entries age correctly across restarts.
    """
    now = time.time()
    hit = _cache.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]

    if disk:
        path = _disk_path(key)
        try:
            if path.exists():
                blob = json.loads(path.read_text(encoding="utf-8"))
                if (now - blob["t"]) < ttl:
                    _cache[key] = (blob["t"], blob["v"])
                    return blob["v"]
        except Exception:
            pass  # corrupt/unreadable cache file — fall through to refetch

    value = producer()
    _cache[key] = (now, value)
    if disk:
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
    return cached(("info", ticker.upper()), produce, ttl=TTL_QUOTE, disk=True)


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
    return cached(("fast_info", ticker.upper()), produce, ttl=TTL_QUOTE)


def get_history(ticker: str, period: str = "2y", interval: str = "1d"):
    """yfinance price history DataFrame. Returns an empty DataFrame on failure."""
    def produce():
        try:
            return yf.Ticker(ticker).history(period=period, interval=interval,
                                              auto_adjust=True)
        except Exception:
            import pandas as pd
            return pd.DataFrame()
    # DataFrames aren't JSON-serializable — memory cache only.
    return cached(("history", ticker.upper(), period, interval), produce,
                  ttl=TTL_HISTORY)


def get_dividends_raw(ticker: str):
    """yfinance dividend payment series. Returns None on failure."""
    def produce():
        try:
            return yf.Ticker(ticker).dividends
        except Exception:
            return None
    return cached(("dividends", ticker.upper()), produce, ttl=TTL_HISTORY)


def get_earnings_dates_raw(ticker: str):
    """yfinance earnings-dates DataFrame (slow endpoint). None on failure."""
    def produce():
        try:
            return yf.Ticker(ticker).earnings_dates
        except Exception:
            return None
    return cached(("earnings_dates", ticker.upper()), produce, ttl=TTL_HISTORY)


def get_news_raw(ticker: str) -> list:
    """Raw yfinance .news list. Returns [] on failure (schema is unstable)."""
    def produce() -> list:
        try:
            news = yf.Ticker(ticker).news
            return news if isinstance(news, list) else []
        except Exception:
            return []
    return cached(("news", ticker.upper()), produce, ttl=TTL_NEWS)


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
