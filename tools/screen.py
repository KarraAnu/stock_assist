"""Multi-stock screener / comparator.

Polls a basket of tickers through get_market_data (yfinance — universal
coverage, no FMP gating), derives a cheapness/quality picture for each, ranks
them, and tags each with a plain verdict bucket. This is the "look at a field
and find the cheap quality names" workflow as one call, instead of N manual
lookups.
"""

from __future__ import annotations

from .common import normalize_ticker, num
from .market import get_market_data
from .universe import get_universe

_MAX_TICKERS = 30
# Verdicts excluded from the value/quality sleeve (no fundamental anchor).
_VALUE_EXCLUDE = ("SPECULATIVE", "DISTRESS")


def _row(md: dict) -> dict:
    """Flatten a get_market_data result into screenable fields."""
    price = md.get("price")
    mcap = md.get("market_cap")
    val = md.get("valuation", {})
    fcf = md.get("balance_sheet", {}).get("free_cash_flow")
    margins = md.get("margins", {})
    growth = md.get("growth", {})
    hi = md.get("fifty_two_week", {}).get("high")
    tgt = md.get("analyst", {}).get("target_mean")

    fcf_yield = round(fcf / mcap * 100, 2) if (fcf and mcap) else None
    row = {
        "ticker": md.get("ticker"),
        "sector": md.get("sector"),
        "price": price,
        "market_cap": mcap,
        "forward_pe": num(val.get("forward_pe")),
        "trailing_pe": num(val.get("trailing_pe")),
        "price_to_sales": num(val.get("price_to_sales_ttm")),
        "price_to_book": num(val.get("price_to_book")),
        "fcf_yield_pct": fcf_yield,
        "profit_margin_pct": round(margins["profit"] * 100, 1) if margins.get("profit") is not None else None,
        "revenue_growth_pct": round(growth["revenue"] * 100, 1) if growth.get("revenue") is not None else None,
        "dividend_pct": md.get("dividend_yield"),
        "beta": md.get("beta"),
        "pct_off_52w_high": round((price / hi - 1) * 100, 1) if (price and hi) else None,
        "analyst_upside_pct": round((tgt / price - 1) * 100, 1) if (tgt and price) else None,
    }
    return _sanitize(row)


def _sanitize(r: dict) -> dict:
    """Neutralize implausible values that are almost always data artifacts
    (warrant marks, post-merger errors), so they can't inflate the score.

    Caught in dogfooding: PSKY's 140% FCF yield and ONDS's +252% margin were
    driving real portfolio weights. Real FCF yields top out ~30-40%; real net
    margins ~ -100%..70%. Out-of-range values are zeroed out (not trusted) and
    flagged, so a garbage number quietly demotes the name instead of promoting it.
    """
    flags = []
    fy = r.get("fcf_yield_pct")
    if fy is not None and not (-100 <= fy <= 45):
        flags.append(f"implausible FCF yield {fy}% — ignored (likely data artifact)")
        r["fcf_yield_pct"] = None
    pm = r.get("profit_margin_pct")
    if pm is not None and not (-200 <= pm <= 70):
        flags.append(f"implausible profit margin {pm}% — ignored (likely warrant/one-time)")
        r["profit_margin_pct"] = None
    rg = r.get("revenue_growth_pct")
    if rg is not None and rg > 500:
        flags.append(f"revenue growth {rg}% is off a tiny base — not meaningful")
    if flags:
        r["data_flags"] = flags
    return r


def _verdict(r: dict) -> str:
    """Plain-language bucket from the validated screen logic."""
    fpe = r["forward_pe"]; tpe = r["trailing_pe"]; ps = r["price_to_sales"]
    pb = r["price_to_book"]; fy = r["fcf_yield_pct"]; pm = r["profit_margin_pct"]
    unprofitable = (pm is not None and pm <= 0) and (fy is None or fy <= 0)
    # Negative book equity is distress only WITH losses/cash burn. For a
    # profitable, cash-generative company it's a buyback/intangibles artifact
    # (e.g. AbbVie, McKesson), not distress.
    if pb is not None and pb < 0 and unprofitable:
        return "DISTRESS (negative equity + losses)"
    if (fpe is None or fpe <= 0) and (tpe is None or tpe <= 0):
        if ps is not None and ps > 30:
            return "SPECULATIVE (no earnings, revenue immaterial)"
        return "SPECULATIVE (no/negative earnings)"
    cheap = (fpe is not None and 0 < fpe < 16) or (fy is not None and fy > 6)
    quality = (pm is not None and pm > 0) and (fy is None or fy > 0)
    if cheap and quality:
        return "CHEAP-QUALITY"
    if (fpe is not None and fpe > 40) or (ps is not None and ps > 12):
        return "EXPENSIVE"
    return "FAIR"


def _score(r: dict, rank_by: str) -> float:
    """Continuous confidence score (not coarse buckets) so allocations vary.

    Built from real-valued metrics: earnings yield, FCF yield, margin, growth.
    Higher = more conviction for the chosen objective.
    """
    fpe = r["forward_pe"]; fy = r["fcf_yield_pct"]; pm = r["profit_margin_pct"]
    rg = r["revenue_growth_pct"]; div = r["dividend_pct"]; pb = r["price_to_book"]
    ay = r["analyst_upside_pct"]
    ey = min(100.0 / fpe, 18.0) if (fpe and fpe > 0) else 0.0   # earnings yield %, capped
    fyc = min(max(fy or 0.0, 0.0), 25.0)                        # FCF yield %, floored/capped

    if rank_by == "income":
        s = (min(div, 8.0) if div else 0.0)
        if fy and div and fy > div: s += 2.0                   # FCF covers dividend
        if pm and pm > 0: s += 1.0
        if fy is not None and fy < 0: s -= 3.0
    elif rank_by == "quality":
        s = min(max((pm or 0) / 8.0, -2.0), 5.0) + min(max(rg or 0, 0) / 5.0, 3.0) \
            + min(fyc / 3.0, 4.0) + ey / 4.0
    elif rank_by == "growth":
        s = min(max(rg or 0, 0) / 4.0, 6.0) + min(max(ay or 0, 0) / 15.0, 3.0)
        if pm and pm > 0: s += 1.0
    else:  # "value" (default): earnings yield + FCF yield + quality tilt
        s = ey + fyc
        if pm and pm > 0: s += 1.0
        if rg and rg > 0: s += 1.0
    # Negative equity is a penalty only alongside losses/cash burn (else it's a
    # buyback artifact on a healthy company).
    if pb is not None and pb < 0 and (pm is None or pm <= 0) and (fy is None or fy <= 0):
        s -= 5.0
    return round(s, 2)


def screen_stocks(tickers, rank_by: str = "value") -> dict:
    """Screen and rank a basket of stocks; tag each with a verdict bucket.

    Polls each ticker's market data and returns a table ranked by `rank_by`:
      - "value" (default): cheap on forward P/E + high free-cash-flow yield
      - "quality": strong margins + growth + cash generation
      - "income": dividend yield backed by free cash flow
      - "growth": revenue growth + analyst upside

    Each row includes forward/trailing P/E, P/S, P/B, FCF yield, profit margin,
    revenue growth, dividend, beta, % off the 52-week high, analyst upside, a
    composite score, and a plain verdict (CHEAP-QUALITY / FAIR / EXPENSIVE /
    SPECULATIVE / DISTRESS). Use it to narrow a field, then run the deep tools
    (get_financials, get_quality_scores, reverse_dcf) on the survivors.

    `tickers` may be a list or a comma-separated string (max 30). Uses yfinance,
    so coverage is universal (no FMP gating). Names that fail to fetch are
    listed under "skipped".
    """
    if isinstance(tickers, str):
        tickers = [t for t in tickers.replace(",", " ").split() if t]
    seen = []
    for t in tickers:
        nt = normalize_ticker(t)
        if nt and nt not in seen:
            seen.append(nt)
    seen = seen[:_MAX_TICKERS]
    if not seen:
        return {"error": "no tickers provided"}
    rank_by = rank_by if rank_by in {"value", "quality", "income", "growth"} else "value"

    rows, skipped = [], []
    for t in seen:
        md = get_market_data(t)
        if "error" in md or md.get("price") is None:
            skipped.append(t)
            continue
        r = _row(md)
        r["verdict"] = _verdict(r)
        r["score"] = _score(r, rank_by)
        rows.append(r)

    rows.sort(key=lambda r: r["score"], reverse=True)
    return {
        "ranked_by": rank_by,
        "count": len(rows),
        "results": rows,
        "skipped": skipped,
        "note": "screen narrows the field; confirm survivors with get_financials "
                "(normalized), get_quality_scores (distress), and reverse_dcf.",
    }


def _confidence_weights(scores: list[float], max_w: float = 0.10,
                        power: float = 2.5) -> list[float]:
    """Convert scores into portfolio weights with a CONCENTRATION knob.

    `power` controls the spread: 1.0 is ~proportional; higher values give
    high-conviction names disproportionately more weight (wider distribution).
    `max_w` caps any single name so even top conviction stays bounded.
    """
    raw = [max(s, 0.01) ** power for s in scores]   # convex emphasis on conviction
    total = sum(raw) or 1.0
    w = [x / total for x in raw]
    for _ in range(10):                   # cap + renormalize until within cap
        w = [min(x, max_w) for x in w]
        s = sum(w) or 1.0
        w = [x / s for x in w]
        if max(w) <= max_w + 1e-9:
            break
    return w


# Sleeve config: (universe, rank_by, verdict-prefixes-to-exclude).
# Value drops anything without a fundamental anchor; growth/IPO KEEP speculation
# (that's the point) and only drop genuinely dying names.
_SLEEVES = {
    "value": ("sp500", "value", ("SPECULATIVE", "DISTRESS")),
    "growth": ("growth", "growth", ("DISTRESS",)),
    "ipo": ("ipo", "growth", ("DISTRESS",)),
}


def scan_universe(sleeve: str = "value", top_n: int = 100,
                  max_weight: float = 0.10, power: float = 2.5) -> dict:
    """Scan a sleeve's universe and build a confidence-weighted target portfolio.

    Sleeves:
      - "value": S&P 500, ranked by cheap-quality (low P/E + high FCF yield);
        excludes no-anchor names (SPECULATIVE / DISTRESS).
      - "growth": curated growth/thematic universe (quantum, nuclear, drones,
        AI, fintech, etc.), ranked by revenue growth + analyst upside; KEEPS
        speculative names, drops only the genuinely distressed.
      - "ipo": recently-IPO'd US names (FMP IPO calendar), ranked like growth.

    Takes the top `top_n`, assigns each a confidence weight (score**power,
    capped at `max_weight`). Returns each pick with metrics, score, verdict, and
    target weight. NOTE: the value scan (~500 names) takes a few minutes; growth
    and IPO are faster. Names that fail to fetch are counted under "skipped".
    """
    universe, rank_by, exclude = _SLEEVES.get(sleeve, _SLEEVES["value"])
    tickers = get_universe(universe)

    rows, skipped = [], 0
    for t in tickers:
        md = get_market_data(t)
        if "error" in md or md.get("price") is None:
            skipped += 1
            continue
        r = _row(md)
        r["verdict"] = _verdict(r)
        if any(r["verdict"].startswith(x) for x in exclude):
            continue
        r["score"] = _score(r, rank_by)
        rows.append(r)

    rows.sort(key=lambda r: r["score"], reverse=True)
    picks = rows[: max(1, int(top_n))]
    weights = _confidence_weights([p["score"] for p in picks], max_weight, power)
    for p, w in zip(picks, weights):
        p["target_weight_pct"] = round(w * 100, 2)

    return {
        "sleeve": sleeve,
        "universe": universe,
        "ranked_by": rank_by,
        "scanned": len(tickers),
        "skipped": skipped,
        "eligible_after_filters": len(rows),
        "selected": len(picks),
        "picks": picks,
        "weighting": f"confidence-weighted (power={power}), capped at {max_weight*100:.0f}% per name",
    }

