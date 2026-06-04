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

import math
import os
import time
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
# Tiny in-memory TTL cache (per-process; resets on server restart)
# --------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 60.0
_cache: dict[tuple, tuple[float, Any]] = {}


def _cached(key: tuple, producer: Callable[[], Any]) -> Any:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    value = producer()
    _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    _cache.clear()


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
    return _cached(("info", ticker.upper()), produce)


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
    return _cached(("fast_info", ticker.upper()), produce)


def get_history(ticker: str, period: str = "2y", interval: str = "1d"):
    """yfinance price history DataFrame. Returns an empty DataFrame on failure."""
    def produce():
        try:
            return yf.Ticker(ticker).history(period=period, interval=interval,
                                              auto_adjust=True)
        except Exception:
            import pandas as pd
            return pd.DataFrame()
    return _cached(("history", ticker.upper(), period, interval), produce)


def get_news_raw(ticker: str) -> list:
    """Raw yfinance .news list. Returns [] on failure (schema is unstable)."""
    def produce() -> list:
        try:
            news = yf.Ticker(ticker).news
            return news if isinstance(news, list) else []
        except Exception:
            return []
    return _cached(("news", ticker.upper()), produce)


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
