"""Canonical valuation bases — the single source of truth for price/EPS/FCF.

Field feedback exposed the failure this module exists to kill: three different
P/E values for the same stock on the same day, because each tool picked its own
EPS basis (TTM-with-one-times vs normalized vs fiscal-year-end) and its own
price (live vs FY-end snapshot). Every tool that reports a multiple now derives
it from here and LABELS the basis, so two tools can never silently disagree.

Bases:
  - price: live (yfinance, cross-checked), never an FY-end snapshot
  - EPS:  ttm (includes one-time items), fy_latest, normalized (median FY
          diluted EPS over the window)
  - FCF:  latest_fy, normalized (median FY), with a distortion flag when the
          latest year deviates sharply from the median — the input reverse_dcf
          must respect (a cratered base year once produced a fake "priced for
          50% growth" reading)
"""

from __future__ import annotations

import statistics

from .common import get_fast_info, get_info, normalize_ticker, num
from .fmp import FMPError, fmp_get, have_key

# Latest-year FCF deviating this much from the multi-year median marks the
# base as distorted (one-time collapse/spike) for growth-implication math.
FCF_DISTORTION_THRESHOLD = 0.35


def live_price(ticker: str) -> float | None:
    """Live price from yfinance (.info first, .fast_info fallback)."""
    info = get_info(ticker)
    p = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    if p is None:
        p = num(get_fast_info(ticker).get("last_price"))
    return p


def live_market_cap(ticker: str) -> float | None:
    info = get_info(ticker)
    return num(info.get("marketCap")) or num(get_fast_info(ticker).get("market_cap"))


def eps_bases(ticker: str, years: int = 5) -> dict:
    """The three EPS bases, explicitly labeled. FY series needs FMP; without it
    only the ttm/forward-relevant fields are populated."""
    ticker = normalize_ticker(ticker)
    info = get_info(ticker)
    out: dict = {
        "ttm": num(info.get("trailingEps")),
        "fy_latest": None,
        "normalized_median": None,
        "window_years": 0,
        "basis_notes": {
            "ttm": "trailing-12-month EPS as reported — includes one-time items",
            "normalized_median": "median fiscal-year diluted EPS over the window "
                                 "— resistant to one-time spikes/collapses",
        },
    }
    if not have_key():
        return out
    try:
        income = fmp_get("income-statement", symbol=ticker, period="annual",
                         limit=max(2, min(int(years), 10)))
    except FMPError:
        return out
    series = [num(s.get("epsDiluted")) for s in (income or [])]
    series = [e for e in series if e is not None]
    if series:
        out["fy_latest"] = round(series[0], 2)
        out["normalized_median"] = round(statistics.median(series), 2)
        out["window_years"] = len(series)
    return out


def fcf_bases(ticker: str, years: int = 5) -> dict:
    """Latest-FY vs normalized free cash flow, with distortion/volatility flags.

    The distortion baseline is the median of PRIOR years, not the whole
    series: when the latest year is the outlier, including it in the median
    can make the median equal the outlier (VST: a 129M collapse year was the
    exact median of a series with two negative years — the whole-series
    "normalized" base reproduced the distortion it existed to catch).
    """
    ticker = normalize_ticker(ticker)
    out: dict = {"latest_fy": None, "normalized_median": None,
                 "prior_years_median": None, "deviation_pct": None,
                 "distorted": False, "volatile_series": False, "by_year": {}}
    if not have_key():
        info = get_info(ticker)
        out["latest_fy"] = num(info.get("freeCashflow"))
        return out
    try:
        cash = fmp_get("cash-flow-statement", symbol=ticker, period="annual",
                       limit=max(2, min(int(years), 10)))
    except FMPError:
        return out
    series = []
    for c in cash or []:
        fy, fcf = str(c.get("fiscalYear")), num(c.get("freeCashFlow"))
        if fcf is not None:
            series.append(fcf)
            out["by_year"][fy] = fcf
    if not series:
        return out
    latest = series[0]
    prior = series[1:]
    out["latest_fy"] = latest
    out["normalized_median"] = statistics.median(series)
    out["prior_years_median"] = statistics.median(prior) if prior else None

    sign_flips = sum(1 for a, b in zip(series, series[1:]) if (a > 0) != (b > 0))
    out["volatile_series"] = len(series) >= 3 and sign_flips >= 1
    if out["volatile_series"]:
        out["volatility_note"] = ("FCF flips sign across the window — no single "
                                  "base year is representative; inspect by_year "
                                  "before trusting any growth implication")

    baseline = out["prior_years_median"]
    if baseline and abs(baseline) > 0:
        dev = (latest - baseline) / abs(baseline)
        out["deviation_pct"] = round(dev * 100, 1)
        out["distorted"] = len(series) >= 3 and abs(dev) > FCF_DISTORTION_THRESHOLD
    if out["distorted"]:
        out["note"] = (f"latest FY FCF deviates {out['deviation_pct']}% from the "
                       f"median of prior years — treat latest-FCF-based growth "
                       f"implications as unreliable; prefer the prior-years-median "
                       f"base")
    return out


def pe_bases(ticker: str, years: int = 5) -> dict:
    """All P/E variants from ONE live price and labeled EPS bases.

    This is the block every tool should report instead of inventing its own
    P/E. `forward` uses the analyst-consensus forward EPS (provider-supplied);
    `ttm` is live price / TTM EPS (one-time items included — can mislead after
    a windfall year); `normalized` is live price / median FY EPS.
    """
    ticker = normalize_ticker(ticker)
    info = get_info(ticker)
    price = live_price(ticker)
    eps = eps_bases(ticker, years)

    def ratio(p, e):
        return round(p / e, 1) if (p and e and e > 0) else None

    def r1(x):
        return round(x, 1) if x is not None else None

    return {
        "price": price,
        "forward": r1(num(info.get("forwardPE"))),
        "ttm": r1(num(info.get("trailingPE"))) or ratio(price, eps["ttm"]),
        "normalized": ratio(price, eps["normalized_median"]),
        "eps": eps,
        "as_of": "live price; forward = analyst-consensus estimate, ttm = "
                 "trailing-12mo reported EPS (incl. one-times), normalized = "
                 "median FY EPS over the window",
    }
