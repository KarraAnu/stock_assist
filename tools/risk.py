"""Price-based risk metrics (yfinance, no API key needed)."""

from __future__ import annotations

import numpy as np

from .common import get_history, missing_data_error, normalize_ticker


def risk_metrics(ticker: str, period: str = "3y") -> dict:
    """Price-risk profile for a single stock (default 3-year window).

    Returns, from daily price history:
      - annualized_return_pct: compound annual growth rate over the window
      - annualized_volatility_pct: how much the price swings (std of returns)
      - max_drawdown_pct: the worst peak-to-trough fall in the window (the
        deepest loss you'd have ridden through)
      - return_per_unit_risk: annualized return ÷ volatility (a Sharpe-style
        ratio with risk-free rate assumed 0 — higher is better reward-for-risk)
      - downside_volatility_pct: volatility of only the down days
      - best_day_pct / worst_day_pct: largest single-day moves
    Plus plain-language flags. Returns {"error": ...} if no price history.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    hist = get_history(ticker, period=period)
    if hist is None or hist.empty or "Close" not in hist:
        return missing_data_error(ticker, ["price_history"], partial={"period": period})

    close = hist["Close"].dropna()
    if len(close) < 30:
        return missing_data_error(ticker, ["sufficient_price_history"],
                                  partial={"period": period, "days": len(close)})

    daily = close.pct_change().dropna()
    trading_days = len(daily)
    years = trading_days / 252 if trading_days else None

    cagr = None
    if years and years > 0 and close.iloc[0] > 0:
        cagr = (close.iloc[-1] / close.iloc[0]) ** (1 / years) - 1

    ann_vol = float(daily.std() * np.sqrt(252))
    downside = daily[daily < 0]
    downside_vol = float(downside.std() * np.sqrt(252)) if len(downside) > 1 else None

    running_max = close.cummax()
    drawdown = (close / running_max - 1.0)
    max_dd = float(drawdown.min())

    rpr = (cagr / ann_vol) if (cagr is not None and ann_vol > 0) else None

    flags = []
    if max_dd <= -0.5:
        flags.append(f"survived a {abs(max_dd) * 100:.0f}% peak-to-trough drawdown in the window")
    if ann_vol >= 0.5:
        flags.append(f"high volatility ({ann_vol * 100:.0f}%/yr) — large swings")
    elif ann_vol <= 0.2:
        flags.append(f"relatively calm ({ann_vol * 100:.0f}%/yr volatility)")
    if rpr is not None and rpr >= 1.0:
        flags.append(f"strong reward-for-risk (return/vol = {rpr:.2f})")
    elif rpr is not None and rpr < 0:
        flags.append("negative return over the window despite the risk taken")

    return {
        "ticker": ticker,
        "period": period,
        "window_years": round(years, 2) if years else None,
        "annualized_return_pct": round(cagr * 100, 2) if cagr is not None else None,
        "annualized_volatility_pct": round(ann_vol * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "return_per_unit_risk": round(rpr, 2) if rpr is not None else None,
        "downside_volatility_pct": round(downside_vol * 100, 2) if downside_vol else None,
        "best_day_pct": round(float(daily.max()) * 100, 2),
        "worst_day_pct": round(float(daily.min()) * 100, 2),
        "flags": flags,
        "note": "return_per_unit_risk assumes a 0% risk-free rate (simplified Sharpe)",
    }
