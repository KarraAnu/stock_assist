"""Discovery: signal-first screens, theme expansion, value-chain comparison.

The old flow was top-down: the user names a sector and the tools search
inside it — so the candidate pool was bounded by whatever the user (or the
model's memory) could name. These tools invert that:

  - discover_stocks: scan the WHOLE US market for signal patterns (fallen
    growth, backlog inflection, quiet compounders) and return candidates with
    the reason each fired — the sector falls out of the results.
  - expand_theme: the ETF industry has already mapped most themes to
    constituents; union the holdings of theme-matching funds into a ranked
    candidate list (automates deep-dive Phase 0).
  - compare_baskets: given value-chain nodes (e.g. AI → chips → power →
    copper), measure which nodes have already re-rated and which lag — the
    "copper trade" screen. The chain map comes from the judgment layer; this
    finds the unpriced link.
"""

from __future__ import annotations

import json

from .common import normalize_ticker, num, parallel_map
from .filings import get_rpo
from .fmp import FMPError, fmp_get, have_key
from .market import get_etf_info, get_market_data
from .universe import get_universe

_US_EXCHANGES = ("NASDAQ", "NYSE", "NEW YORK", "AMEX", "AMERICAN")


def _us_symbol(sym: str, exchange: str) -> bool:
    exch = (exchange or "").upper()
    return (bool(sym) and sym.isalpha() and len(sym) <= 5
            and any(x in exch for x in _US_EXCHANGES))


def _market_pool(min_cap: float = 3e8, max_names: int = 600) -> list[str]:
    """Broad US pool from the FMP screener, paged by cap band so mid-caps
    aren't crowded out by the mega-cap top 100."""
    bands = [(min_cap, 2e9), (2e9, 10e9), (10e9, 50e9), (50e9, None)]
    out, seen = [], set()
    for lo, hi in bands:
        params = {"marketCapMoreThan": int(lo), "isEtf": "false",
                  "isActivelyTrading": "true", "volumeMoreThan": 100_000,
                  "limit": 200}
        if hi:
            params["marketCapLowerThan"] = int(hi)
        try:
            rows = fmp_get("company-screener", **params)
        except FMPError:
            continue
        for r in rows if isinstance(rows, list) else []:
            sym = str(r.get("symbol") or "").upper().strip()
            if sym not in seen and _us_symbol(sym, str(r.get("exchange") or "")):
                seen.add(sym)
                out.append(sym)
    return out[:max_names]


def _md_rows(tickers: list[str]) -> list[dict]:
    """Parallel market-data fetch, keeping only usable stock rows."""
    rows = []
    for md in parallel_map(get_market_data, tickers, workers=8):
        if (isinstance(md, dict) and "error" not in md
                and md.get("price") is not None
                and md.get("instrument_type") not in ("ETF", "MUTUALFUND")):
            rows.append(md)
    return rows


def _brief(md: dict, reason: str) -> dict:
    val = md.get("valuation", {})
    hi = md.get("fifty_two_week", {}).get("high")
    price = md.get("price")
    growth = md.get("growth", {}).get("revenue")
    fcf = md.get("balance_sheet", {}).get("free_cash_flow")
    mcap = md.get("market_cap")
    return {
        "ticker": md.get("ticker"),
        "name": md.get("name"),
        "sector": md.get("sector"),
        "industry": md.get("industry"),
        "market_cap": mcap,
        "pct_off_52w_high": (round((price / hi - 1) * 100, 1)
                             if (price and hi) else None),
        "revenue_growth_pct": (round(growth * 100, 1)
                               if growth is not None else None),
        "fcf_yield_pct": (round(fcf / mcap * 100, 2)
                          if (fcf and mcap) else None),
        "forward_pe": num(val.get("forward_pe")),
        "reason": reason,
    }


def discover_stocks(signal: str = "fallen_growth", limit: int = 25) -> dict:
    """Scan the market for a signal pattern — discovery without naming a sector.

    Signals:
      - "fallen_growth": 40%+ off the 52-week high with revenue still growing
        20%+ — post-bubble digestion setups where the business kept compounding
        while the price derated. Scans the full US market (>$300M cap).
      - "quiet_compounders": FCF yield >= 5%, revenue growth >= 5%, profitable,
        with thin analyst coverage (<= 8) — quality nobody is watching.
        Scans S&P 500 + the growth universe.
      - "backlog_inflection": contracted backlog (RPO, SEC XBRL) growing 50%+
        YoY and material vs market cap — the IonQ pattern, visible quarters
        before the income statement moves. Scans the growth universe.

    Returns candidates with the reason each fired. This narrows the field
    only — run analyze_stocks on the interesting ones. Full-market sweeps
    take ~1-3 minutes on a cold cache, much less warm.
    """
    signal = (signal or "").strip().lower()
    n = max(1, int(limit))

    if signal == "fallen_growth":
        pool = _market_pool()
        if not pool:
            return {"error": "screener pool came back empty — check FMP key/tier"}
        results = []
        for md in _md_rows(pool):
            row = _brief(md, "")
            off, g = row["pct_off_52w_high"], row["revenue_growth_pct"]
            if off is None or g is None:
                continue
            if off <= -40 and g >= 20:
                row["reason"] = (f"{abs(off):.0f}% off its high with revenue "
                                 f"still growing {g:.0f}%")
                results.append(row)
        results.sort(key=lambda r: (r["revenue_growth_pct"] or 0), reverse=True)
        return {"signal": signal, "scanned": len(pool),
                "count": min(len(results), n), "results": results[:n],
                "note": "price broke, business didn't (on trailing data) — "
                        "verify the thesis per name with analyze_stocks; some "
                        "of these fell for good reasons"}

    if signal == "quiet_compounders":
        pool = list(dict.fromkeys(get_universe("sp500") + get_universe("growth")))
        results = []
        for md in _md_rows(pool):
            row = _brief(md, "")
            fy, g = row["fcf_yield_pct"], row["revenue_growth_pct"]
            margin = md.get("margins", {}).get("profit")
            analysts = num(md.get("analyst", {}).get("num_analysts"))
            if fy is None or g is None or margin is None:
                continue
            if fy >= 5 and g >= 5 and margin > 0 and (analysts or 0) <= 8:
                row["analyst_count"] = analysts
                row["reason"] = (f"{fy:.1f}% FCF yield, {g:.0f}% growth, "
                                 f"{int(analysts or 0)} analysts covering")
                results.append(row)
        results.sort(key=lambda r: (r["fcf_yield_pct"] or 0), reverse=True)
        return {"signal": signal, "scanned": len(pool),
                "count": min(len(results), n), "results": results[:n],
                "note": "cheap cash machines with thin coverage — check "
                        "WHY coverage is thin (size? structure? a known flaw?)"}

    if signal == "backlog_inflection":
        pool = get_universe("growth")
        hits = []
        for t, rpo in zip(pool, parallel_map(get_rpo, pool, workers=8)):
            if not (isinstance(rpo, dict) and rpo.get("available")):
                continue
            g = rpo.get("yoy_growth_pct")
            latest = (rpo.get("latest") or {}).get("value")
            if g is None or latest is None or g < 50:
                continue
            hits.append((t, g, latest))
        results = []
        for md in _md_rows([h[0] for h in hits]):
            t = md.get("ticker")
            g, latest = next((h[1], h[2]) for h in hits if h[0] == t)
            mcap = md.get("market_cap")
            ratio = (latest / mcap * 100) if (mcap and latest) else None
            if ratio is None or ratio < 1.0:
                continue  # backlog must be material vs the cap, not a rounding error
            row = _brief(md, f"contracted backlog (RPO) +{g:.0f}% YoY to "
                             f"${latest / 1e6:,.0f}M ({ratio:.1f}% of mcap)")
            row["rpo_usd_m"] = round(latest / 1e6, 1)
            row["rpo_yoy_pct"] = g
            row["rpo_vs_mcap_pct"] = round(ratio, 1)
            results.append(row)
        results.sort(key=lambda r: r["rpo_yoy_pct"], reverse=True)
        return {"signal": signal, "scanned": len(pool),
                "count": min(len(results), n), "results": results[:n],
                "note": "backlog compounding ahead of revenue — the income "
                        "statement catches up later; verify contract quality "
                        "(who signed, cancellation terms) per name"}

    return {"error": f"unknown signal '{signal}' — use fallen_growth, "
                     f"quiet_compounders, or backlog_inflection"}


def expand_theme(theme: str, max_etfs: int = 6) -> dict:
    """Turn a loose theme into a ranked candidate list via ETF holdings.

    The ETF industry has already mapped most investable themes to
    constituents. This finds funds whose names match the theme ("quantum",
    "uranium", "defense", "robotics"...), unions their top holdings, and
    ranks each name by how many theme funds hold it and at what combined
    weight. Automates the candidate-generation phase of a sector deep-dive —
    feed the result to screen_stocks / analyze_stocks.
    """
    theme = (theme or "").strip()
    if not theme:
        return {"error": "no theme provided"}
    if not have_key():
        return {"error": "FMP_API_KEY not set — theme search needs FMP"}

    try:
        all_etfs = fmp_get("etf-list")  # ~6,100 funds; cached 24h on disk
    except FMPError as exc:
        return {"error": f"FMP etf-list failed: {str(exc)[:140]}"}

    tl = theme.lower()
    # Skip leveraged/inverse wrappers — their holdings are swaps, not stocks.
    _LEVERED = ("2X", "3X", "-1X", "1.5X", "INVERSE", "BEAR", "ULTRA ",
                "DAILY TARGET")
    matches = []
    for r in all_etfs if isinstance(all_etfs, list) else []:
        sym = str(r.get("symbol") or "").upper().strip()
        name = str(r.get("name") or "")
        if (tl in name.lower() and sym.isalpha() and len(sym) <= 5
                and not any(x in name.upper() for x in _LEVERED)):
            matches.append({"symbol": sym, "name": name})
    if not matches:
        return {"theme": theme, "etfs_used": [], "candidates": [],
                "note": f"no US-listed ETFs matched '{theme}' by name — try a "
                        f"broader word, or name tickers directly"}

    # Prefer the largest funds: fetch profiles for up to 12 matches, keep the
    # max_etfs biggest by AUM (size = the market's vote on the canonical
    # vehicle for the theme).
    probe = matches[:12]
    probed = parallel_map(lambda e: get_etf_info(e["symbol"]), probe, workers=6)
    sized = [(e, info) for e, info in zip(probe, probed)
             if isinstance(info, dict) and "error" not in info and info.get("aum")]
    sized.sort(key=lambda x: x[1]["aum"], reverse=True)
    etfs = [e for e, _ in sized[:max(1, int(max_etfs))]]
    if not etfs:
        return {"theme": theme, "etfs_used": [], "candidates": [],
                "note": f"matched {len(matches)} fund name(s) for '{theme}' "
                        f"but none returned usable profiles"}

    candidates: dict[str, dict] = {}
    infos = parallel_map(lambda e: get_etf_info(e["symbol"]), etfs, workers=4)
    used = []
    for e, info in zip(etfs, infos):
        if not isinstance(info, dict) or "error" in info:
            continue
        used.append({"symbol": e["symbol"], "name": info.get("name") or e["name"],
                     "aum": info.get("aum"),
                     "expense_ratio_pct": info.get("expense_ratio_pct")})
        for h in info.get("top_holdings") or []:
            sym = str(h.get("symbol") or "").upper()
            if not sym:
                continue
            c = candidates.setdefault(sym, {"ticker": sym,
                                            "name": h.get("name"),
                                            "held_by": [],
                                            "combined_weight_pct": 0.0})
            c["held_by"].append(e["symbol"])
            c["combined_weight_pct"] = round(
                c["combined_weight_pct"] + (h.get("weight_pct") or 0.0), 2)

    ranked = sorted(candidates.values(),
                    key=lambda c: (len(c["held_by"]), c["combined_weight_pct"]),
                    reverse=True)
    for c in ranked:
        c["fund_count"] = len(c.pop("held_by"))
    return {
        "theme": theme,
        "etfs_used": used,
        "candidates": ranked,
        "note": "holdings are providers' top-10s only (union, not full "
                "constituents); non-US listings (e.g. .TO) need local-listing "
                "equivalents for the US tools. Screen the list next with "
                "screen_stocks, then analyze_stocks.",
    }


def compare_baskets(groups) -> dict:
    """Measure which value-chain nodes have re-rated and which still lag.

    `groups` maps node names to ticker lists, e.g.
    {"ai_chips": ["NVDA","AMD"], "power": ["CEG","VST"], "copper": ["FCX","SCCO"]}
    (a JSON string is also accepted). For each node: median 1m/3m/6m/1y
    performance, forward P/E, and % off the 52-week high.

    This is the second-order-trend screen: the judgment layer maps the causal
    chain (boom → enablers → inputs → infrastructure); this shows where the
    market has already paid up and which links it hasn't repriced yet. A
    lagging node is only a candidate if the chain logic actually transmits
    demand to it — that part stays your call.
    """
    if isinstance(groups, str):
        try:
            groups = json.loads(groups)
        except ValueError:
            return {"error": "groups must be a JSON object: "
                             "{\"node\": [\"TICKER\", ...], ...}"}
    if not isinstance(groups, dict) or not groups:
        return {"error": "groups must map node names to ticker lists"}

    import statistics

    def median(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(statistics.median(vals), 1) if vals else None

    from .indicators import get_indicators

    nodes = []
    for node, tickers in groups.items():
        if isinstance(tickers, str):
            tickers = [t for t in tickers.replace(",", " ").split() if t]
        tickers = [normalize_ticker(t) for t in tickers if normalize_ticker(t)][:10]
        mds = _md_rows(tickers)
        inds = parallel_map(lambda t: get_indicators(t, period="2y"),
                            tickers, workers=8)
        perf = {k: [] for k in ("1m", "3m", "6m", "1y")}
        for ind in inds:
            if isinstance(ind, dict) and "error" not in ind:
                for k in perf:
                    perf[k].append((ind.get("performance_pct") or {}).get(k))
        off_high, fwd_pe = [], []
        for md in mds:
            hi = md.get("fifty_two_week", {}).get("high")
            price = md.get("price")
            if price and hi:
                off_high.append((price / hi - 1) * 100)
            fwd_pe.append(num(md.get("valuation", {}).get("forward_pe")))
        nodes.append({
            "node": str(node),
            "tickers_ok": len(mds),
            "median_perf_pct": {k: median(v) for k, v in perf.items()},
            "median_forward_pe": median([p for p in fwd_pe if p and p > 0]),
            "median_pct_off_52w_high": median(off_high),
        })

    nodes.sort(key=lambda x: x["median_perf_pct"].get("6m") or -999,
               reverse=True)
    lagged = [x["node"] for x in nodes[-2:]] if len(nodes) >= 3 else []
    return {
        "nodes": nodes,
        "ranked_by": "median 6-month performance (most re-rated first)",
        "least_rerated": lagged,
        "note": "a lagging node is only an opportunity if the chain logic "
                "transmits demand to it — verify the causal link, then "
                "analyze_stocks the node's names",
    }
