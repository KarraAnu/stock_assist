"""Technical indicators + (current-only) valuation context."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import (
    get_history,
    missing_data_error,
    normalize_ticker,
    num,
)

_TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder's smoothing.
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / last_loss
    return float(round(100 - (100 / (1 + rs)), 2))


def _pct_perf(close: pd.Series, days: int) -> float | None:
    if len(close) <= days:
        return None
    past = close.iloc[-(days + 1)]
    if past == 0:
        return None
    return float(round((close.iloc[-1] / past - 1) * 100, 2))


def get_indicators(ticker: str, period: str = "2y") -> dict:
    """Compute technical indicators from price history for a single stock.

    Returns RSI(14), SMA 20/50/200, % above SMA50/SMA200, Bollinger-band
    position (0 = lower band, 1 = upper band), realized volatility (30-day and
    1-year, annualized), trailing performance over 1m/3m/6m/1y, and recent
    volume vs the 50-day average. Also returns a list of plain-language "flags"
    (e.g. "RSI overbought at 78", "up 110% over the past year") that highlight
    notable conditions.

    Indicators requiring more history than is available (e.g. SMA200 on a newly
    listed stock) are returned as null with the reason noted. If no price
    history is available at all, returns an {"error": ...} payload.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    hist = get_history(ticker, period=period)
    if hist is None or hist.empty or "Close" not in hist:
        return missing_data_error(ticker, ["price_history"], partial={"period": period})

    close = hist["Close"].dropna()
    if close.empty:
        return missing_data_error(ticker, ["price_history"], partial={"period": period})

    price = float(close.iloc[-1])
    volume = hist["Volume"].dropna() if "Volume" in hist else pd.Series(dtype=float)

    notes = []
    flags = []

    def sma(window: int) -> float | None:
        if len(close) < window:
            notes.append(f"sma_{window} needs {window} days, have {len(close)}")
            return None
        return float(round(close.tail(window).mean(), 4))

    sma20, sma50, sma200 = sma(20), sma(50), sma(200)

    def pct_above(ma: float | None) -> float | None:
        if ma is None or ma == 0:
            return None
        return float(round((price / ma - 1) * 100, 2))

    pct_above_50 = pct_above(sma50)
    pct_above_200 = pct_above(sma200)

    # Bollinger position (20-day, 2 std).
    boll_pos = None
    if len(close) >= 20:
        window = close.tail(20)
        mid = window.mean()
        std = window.std()
        if std and std > 0:
            upper, lower = mid + 2 * std, mid - 2 * std
            boll_pos = float(round((price - lower) / (upper - lower), 3))

    # Realized volatility, annualized.
    log_ret = np.log(close / close.shift(1)).dropna()
    vol_30d = vol_1y = None
    if len(log_ret) >= 30:
        vol_30d = float(round(log_ret.tail(30).std() * np.sqrt(252) * 100, 2))
    if len(log_ret) >= 252:
        vol_1y = float(round(log_ret.tail(252).std() * np.sqrt(252) * 100, 2))
    elif len(log_ret) >= 30:
        vol_1y = float(round(log_ret.std() * np.sqrt(252) * 100, 2))
        notes.append(f"1y volatility uses {len(log_ret)} days (<252 available)")

    rsi = _rsi(close)
    perf = {k: _pct_perf(close, d) for k, d in _TRADING_DAYS.items()}

    # Recent volume vs 50-day average.
    vol_ratio = None
    if len(volume) >= 50:
        avg50 = volume.tail(50).mean()
        if avg50 and avg50 > 0:
            vol_ratio = float(round(volume.iloc[-1] / avg50, 2))

    # ----- plain-language flags -----
    if rsi is not None:
        if rsi >= 70:
            flags.append(f"RSI overbought at {rsi:.0f}")
        elif rsi <= 30:
            flags.append(f"RSI oversold at {rsi:.0f}")
    if pct_above_200 is not None:
        if pct_above_200 > 0 and sma50 is not None and sma200 is not None and sma50 > sma200:
            flags.append("trading above rising SMA50/SMA200 (uptrend)")
        elif pct_above_200 < -10:
            flags.append(f"{abs(pct_above_200):.0f}% below SMA200 (downtrend)")
    if perf.get("1y") is not None and perf["1y"] >= 50:
        flags.append(f"up {perf['1y']:.0f}% over the past year")
    if perf.get("1y") is not None and perf["1y"] <= -30:
        flags.append(f"down {abs(perf['1y']):.0f}% over the past year")
    if boll_pos is not None and boll_pos >= 1.0:
        flags.append("price at/above upper Bollinger band")
    if boll_pos is not None and boll_pos <= 0.0:
        flags.append("price at/below lower Bollinger band")
    if vol_ratio is not None and vol_ratio >= 2.0:
        flags.append(f"volume {vol_ratio:.1f}x the 50-day average")

    result = {
        "ticker": ticker,
        "period": period,
        "price": float(round(price, 4)),
        "rsi_14": rsi,
        "sma": {"sma20": sma20, "sma50": sma50, "sma200": sma200},
        "pct_above_sma50": pct_above_50,
        "pct_above_sma200": pct_above_200,
        "bollinger_position": boll_pos,
        "realized_volatility_pct": {"d30_annualized": vol_30d, "y1_annualized": vol_1y},
        "performance_pct": perf,
        "volume_vs_50d_avg": vol_ratio,
        "flags": flags,
    }
    if notes:
        result["notes"] = notes
    return result


# get_valuation_context was removed in v4: it reported a third, unlabeled P/E
# basis that contradicted the canonical bases in tools/core.py. Use
# get_market_data (basis-labeled) or get_historical_valuation instead.
