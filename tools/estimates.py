"""Forward analyst estimates + revision momentum.

Trailing data answers "what happened"; this layer answers "what does the
Street expect, and which way are those expectations MOVING" — estimate
revisions are among the most robustly documented return factors. Three
sources, each failing soft:
  - FMP analyst-estimates: multi-year forward revenue/EPS consensus
  - yfinance eps_trend: the same consensus 7/30/60/90 days ago (revision
    momentum)
  - FMP price-target-summary: average analyst target by recency window
    (target momentum)

What this cannot supply: management guidance (lives in press releases /
calls — WebSearch). The guidance-vs-consensus gap stays judgment work.
"""

from __future__ import annotations

from datetime import date

import yfinance as yf

from .common import TTL_QUOTE, cached, get_info, normalize_ticker, num
from .fmp import FMPError, fmp_get, have_key


def _eps_trend(ticker: str):
    """yfinance eps_trend DataFrame (consensus now vs 7/30/60/90d ago)."""
    def produce():
        try:
            df = yf.Ticker(ticker).eps_trend
            return df if df is not None and not df.empty else None
        except Exception:
            return None
    return cached(("eps_trend", ticker.upper()), produce, ttl=TTL_QUOTE,
                  validate=lambda v: v is not None)


def _rev_pct(cur, ago) -> float | None:
    """Signed revision: positive = estimate improved. (cur-ago)/|ago| so a
    widening loss reads DOWN — plain cur/ago flips sign on negative EPS."""
    cur, ago = num(cur), num(ago)
    if cur is None or not ago:
        return None
    return round((cur - ago) / abs(ago) * 100, 1)


def get_estimates(ticker: str) -> dict:
    """Forward consensus estimates and which way they're being revised.

    Returns the next two fiscal years' consensus revenue and EPS (with
    analyst counts), forward growth rates, forward PEG (forward P/E divided
    by FY1→FY2 EPS growth), 30/90-day EPS estimate revisions for the current
    and next fiscal year, and price-target momentum (last-month vs
    last-quarter average target). Rising estimates with a flat price is the
    classic setup; falling estimates under a rising price is the warning.

    Guidance-vs-consensus is NOT here — guidance lives in press releases;
    pull it via WebSearch and compare against these numbers. Sections fail
    soft; sparsely-covered names return what exists.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    out: dict = {"ticker": ticker}
    flags: list[str] = []

    # --- forward consensus (FMP) ---
    fy1 = fy2 = None
    if have_key():
        try:
            rows = fmp_get("analyst-estimates", symbol=ticker,
                           period="annual", limit=6)
        except FMPError:
            rows = []
        today = date.today().isoformat()
        future = sorted((r for r in rows or []
                         if str(r.get("date") or "") >= today),
                        key=lambda r: r["date"])

        def fy(r):
            return {
                "fy_end": r.get("date"),
                "revenue_avg_usd_m": (round(num(r.get("revenueAvg")) / 1e6)
                                      if num(r.get("revenueAvg")) else None),
                "eps_avg": num(r.get("epsAvg")),
                "analysts_eps": num(r.get("numAnalystsEps")),
            }

        if future:
            fy1 = fy(future[0])
            out["fy1"] = fy1
        if len(future) > 1:
            fy2 = fy(future[1])
            out["fy2"] = fy2
    else:
        out["note"] = "FMP_API_KEY not set — forward consensus unavailable"

    if fy1 and fy2:
        r1, r2 = fy1["revenue_avg_usd_m"], fy2["revenue_avg_usd_m"]
        e1, e2 = fy1["eps_avg"], fy2["eps_avg"]
        if r1 and r2:
            out["fwd_revenue_growth_pct"] = round((r2 / r1 - 1) * 100, 1)
        if e1 and e2 and e1 > 0:
            g = (e2 / e1 - 1) * 100
            out["fwd_eps_growth_pct"] = round(g, 1)
            fpe = num(get_info(ticker).get("forwardPE"))
            if fpe and fpe > 0 and g > 0:
                peg = round(fpe / g, 2)
                out["fwd_peg"] = peg
                if peg < 1:
                    flags.append(f"forward PEG {peg} — consensus growth is "
                                 f"cheaper than the multiple suggests")

    # --- estimate-revision momentum (yfinance eps_trend) ---
    trend = _eps_trend(ticker)
    if trend is not None:
        revisions = {}
        for period, label in (("0y", "current_fy"), ("+1y", "next_fy")):
            if period in trend.index:
                row = trend.loc[period]
                revisions[label] = {
                    "eps_now": num(row.get("current")),
                    "revision_30d_pct": _rev_pct(row.get("current"),
                                                 row.get("30daysAgo")),
                    "revision_90d_pct": _rev_pct(row.get("current"),
                                                 row.get("90daysAgo")),
                }
        if revisions:
            out["eps_revisions"] = revisions
            r90 = (revisions.get("next_fy") or {}).get("revision_90d_pct")
            if r90 is None:
                r90 = (revisions.get("current_fy") or {}).get("revision_90d_pct")
            if r90 is not None and abs(r90) >= 3:
                flags.append(f"consensus EPS revised {'UP' if r90 > 0 else 'DOWN'} "
                             f"{abs(r90):.0f}% over 90 days — revision momentum "
                             f"is one of the strongest documented factors")

    # --- price-target momentum (FMP) ---
    if have_key():
        try:
            pts = fmp_get("price-target-summary", symbol=ticker)
            p = pts[0] if isinstance(pts, list) and pts else {}
        except FMPError:
            p = {}
        m, q = num(p.get("lastMonthAvgPriceTarget")), num(p.get("lastQuarterAvgPriceTarget"))
        if m and q:
            out["price_target"] = {
                "last_month_avg": m,
                "last_quarter_avg": q,
                "momentum_pct": round((m / q - 1) * 100, 1),
                "last_month_count": num(p.get("lastMonthCount")),
            }

    out["flags"] = flags
    out["source"] = "FMP analyst-estimates / price-target-summary + yfinance eps_trend"
    return out
