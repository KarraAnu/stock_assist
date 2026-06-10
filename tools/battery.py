"""analyze_stocks — the one-call deep battery for a basket of names.

The sector-deep-dive workflow used to mean 7-10 tool calls per name × N names
(~100 round trips, tens of thousands of context tokens). This tool runs the
entire battery server-side, concurrently, and returns ONE compact scorecard
per ticker. The single-stock tools remain for drill-down; this is the
workhorse for comparing a field.

Facts and deterministic flags only — ranking/judgment stays with the caller.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .common import data_dir, normalize_ticker, num, parallel_map
from .filings import get_rpo
from .fundamentals import (
    get_financials,
    get_historical_valuation,
    get_peers,
    get_quality_scores,
    reverse_dcf,
)
from .indicators import get_indicators
from .market import (
    get_dividend_info,
    get_earnings_calendar,
    get_market_data,
    get_recent_news,
)
from .ownership import get_ownership
from .risk import risk_metrics

_MAX_FULL = 20


def _ok(payload: dict) -> bool:
    return isinstance(payload, dict) and "error" not in payload


def _r(x, ndigits: int = 1) -> float | None:
    v = num(x)
    return round(v, ndigits) if v is not None else None


def _etf_row(md: dict) -> dict:
    """Compact fund scorecard (funds get fund-shaped facts, not stock ratios)."""
    return {
        "ticker": md.get("ticker"),
        "instrument": "fund",
        "name": md.get("name"),
        "category": md.get("category"),
        "price": md.get("price"),
        "aum": md.get("aum"),
        "expense_ratio_pct": md.get("expense_ratio_pct"),
        "distribution_yield_pct": md.get("distribution_yield_pct"),
        "performance_pct": md.get("performance_pct"),
        "top_holdings": (md.get("top_holdings") or [])[:5],
        "sector_weights_pct": md.get("sector_weights_pct"),
        "note": "fund — evaluate on mandate/holdings/cost, not stock ratios; "
                "deep-dive its top holdings with the stock battery",
    }


def _analyze_one(ticker: str) -> dict:
    """Full battery for one ticker, compacted to the decision-relevant fields."""
    ticker = normalize_ticker(ticker)
    md = get_market_data(ticker)
    if not _ok(md):
        return {"ticker": ticker, "error": md.get("error", "market data unavailable")}
    if md.get("instrument_type") in ("ETF", "MUTUALFUND"):
        return _etf_row(md)

    gaps: list[str] = []

    def run(name: str, fn, *args, **kwargs) -> dict:
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — one tool must not kill the row
            gaps.append(f"{name}: {type(exc).__name__}")
            return {}
        if not _ok(out):
            gaps.append(f"{name}: {str(out.get('error', ''))[:80]}")
            return out if isinstance(out, dict) else {}
        return out

    fin = run("financials", get_financials, ticker, years=5)
    q = run("quality", get_quality_scores, ticker)
    hv = run("historical_valuation", get_historical_valuation, ticker, years=5)
    pr = run("peers", get_peers, ticker, limit=5)
    rd = run("reverse_dcf", reverse_dcf, ticker)
    rk = run("risk", risk_metrics, ticker, period="3y")
    ind = run("indicators", get_indicators, ticker, period="2y")
    dv = run("dividend", get_dividend_info, ticker)
    ec = run("earnings", get_earnings_calendar, ticker)
    own = run("ownership", get_ownership, ticker)
    nw = run("news", get_recent_news, ticker, limit=3)

    price, mcap = md.get("price"), md.get("market_cap")
    val = md.get("valuation", {})
    hi = md.get("fifty_two_week", {}).get("high")
    fcf = md.get("balance_sheet", {}).get("free_cash_flow")
    tgt = md.get("analyst", {}).get("target_mean")
    hv_norm = hv.get("normalized", {}) or {}
    hv_mult = hv.get("multiples", {}) or {}

    def position(label: str):
        node = hv_mult.get(label)
        return node.get("position_in_range") if isinstance(node, dict) else None

    peer_med_pe = pr.get("peer_median_pe")
    target_pe = pr.get("target_pe")
    vs_peers_pct = (round((target_pe / peer_med_pe - 1) * 100, 1)
                    if (target_pe and peer_med_pe and peer_med_pe > 0) else None)

    distress = q.get("distress_composite", {}) or {}
    stage = (fin.get("company_stage", {}) or {})
    runway = stage.get("cash_runway") or {}
    fcf_by_year = ((rd.get("fcf_bases", {}) or {}).get("by_year")) or {}
    rk_bench = rk.get("vs_benchmark") or {}
    insider = (own.get("insider_activity", {}) or {})

    # The highest-signal plain-language flags from every layer, deduped in
    # order. These carry the gotchas the conversation layer used to have to
    # remember (distorted FCF base, transitory earnings, Z-score artifacts...).
    # financials.trends is NOT merged here — it's already its own field.
    flags: list[str] = []
    for src in (hv.get("flags"), q.get("flags"), rd.get("flags"),
                (fin.get("normalized", {}) or {}).get("outlier_years"),
                ind.get("flags"), dv.get("flags"),
                rk.get("flags"), own.get("flags")):
        for f in src or []:
            if f not in flags:
                flags.append(f)
    if ec.get("flag"):
        flags.append(ec["flag"])
    if runway.get("flag"):
        flags.append(runway["flag"])
    if str(runway.get("note", "")).startswith("cash moved"):
        flags.append(runway["note"])
    if (fin.get("quarterly") or {}).get("flag"):
        flags.append(fin["quarterly"]["flag"])

    row = {
        "ticker": ticker,
        "instrument": "stock",
        "name": md.get("name"),
        "sector": md.get("sector"),
        "industry": md.get("industry"),
        "price": price,
        "market_cap": mcap,
        "beta": md.get("beta"),
        "pct_off_52w_high": (round((price / hi - 1) * 100, 1)
                             if (price and hi) else None),
        "analyst_upside_pct": (round((tgt / price - 1) * 100, 1)
                               if (tgt and price) else None),
        "valuation": {
            "pe_forward": _r(val.get("forward_pe")),
            "pe_ttm": _r(val.get("trailing_pe")),
            "pe_normalized": hv_norm.get("pe_normalized"),
            "ps_ttm": _r(val.get("price_to_sales_ttm"), 2),
            "pb": _r(val.get("price_to_book"), 2),
            "fcf_yield_ttm_pct": (round(fcf / mcap * 100, 2)
                                  if (fcf and mcap) else None),
            "earnings_pattern": hv_norm.get("earnings_pattern"),
            "vs_own_history": {  # 0 = cheapest, 1 = richest in the 5y range
                "pe_position": position("pe"),
                "ps_position": position("ps"),
                "pb_position": position("pb"),
            },
            "vs_peers": {
                "peer_median_pe": peer_med_pe,
                "peer_median_ps": pr.get("peer_median_ps"),
                "pe_premium_pct": vs_peers_pct,
                "peers": [p.get("symbol") for p in pr.get("peers", [])],
            },
            "implied_fcf_growth_pct": rd.get("implied_fcf_growth_pct"),
            "fcf_base_used": rd.get("fcf_base_used"),
            "basis": "pe_ttm = live price/TTM EPS (incl. one-times); "
                     "pe_normalized = live price/median 5y FY EPS",
        },
        "quality": {
            "piotroski": q.get("piotroski_f_score"),
            "altman_z": _r(q.get("altman_z_score")),
            "distress_severity": distress.get("severity"),
            "distress_red_flags": distress.get("red_flag_count"),
            "loss_type": distress.get("loss_type"),
            "roic_pct": q.get("return_on_invested_capital_pct"),
            "net_debt_ebitda": _r(q.get("net_debt_to_ebitda")),
            "interest_coverage": _r(q.get("interest_coverage")),
        },
        "financials": {
            "stage": stage.get("stage"),
            "trends": fin.get("trends"),
            "fcf_by_year_usd_m": {fy: round(v / 1e6)
                                  for fy, v in fcf_by_year.items()
                                  if v is not None},
            "cash_runway_years": runway.get("runway_years"),
        },
        "risk": {
            "ann_return_pct": rk.get("annualized_return_pct"),
            "ann_vol_pct": rk.get("annualized_volatility_pct"),
            "max_drawdown_pct": rk.get("max_drawdown_pct"),
            "excess_vs_spy_pct": rk_bench.get("excess_return_pct"),
            "realized_beta": rk_bench.get("realized_beta"),
        },
        "momentum": {
            "rsi_14": ind.get("rsi_14"),
            "pct_above_sma200": ind.get("pct_above_sma200"),
            "perf_3m_pct": (ind.get("performance_pct") or {}).get("3m"),
            "perf_1y_pct": (ind.get("performance_pct") or {}).get("1y"),
        },
        "dividend": ({
            "yield_pct": dv.get("dividend_yield_pct"),
            "fcf_coverage": dv.get("fcf_coverage"),
            "cuts_25y": len(dv.get("past_cuts") or []),
        } if dv.get("pays_dividend") else None),
        "next_earnings": {
            "date": ec.get("next_earnings_date"),
            "days_until": ec.get("days_until"),
        } if ec.get("next_earnings_date") else None,
        "ownership": {
            "short_pct_of_float": (own.get("short_interest", {}) or {}).get("short_pct_of_float"),
            "institutional_pct": (own.get("holders", {}) or {}).get("institutional_pct"),
            "insider_net_6m_usd": insider.get("net_value") if insider.get("available") else None,
        },
        "news": [{"title": i.get("title"), "published": i.get("published")}
                 for i in (nw.get("items") or [])],
        "flags": flags,
    }

    quarterly = fin.get("quarterly") or {}
    if quarterly.get("revenue_by_quarter_usd_m"):
        row["financials"]["revenue_by_quarter_usd_m"] = \
            quarterly["revenue_by_quarter_usd_m"]

    # Contracted backlog (RPO) from SEC XBRL — decisive for contract-driven
    # names. Missing/untagged is normal and not a data gap.
    try:
        rpo = get_rpo(ticker)
    except Exception:  # noqa: BLE001
        rpo = {}
    if rpo.get("available"):
        row["rpo"] = {"latest_usd_m": rpo["latest"]["usd_m"],
                      "as_of": rpo["latest"]["end"],
                      "yoy_growth_pct": rpo.get("yoy_growth_pct")}
        g = rpo.get("yoy_growth_pct")
        if g is not None and g >= 50:
            row["flags"].append(f"contracted backlog (RPO) grew {g:.0f}% YoY "
                                f"to ${rpo['latest']['usd_m']:,.0f}M — see "
                                f"get_rpo for the series")

    if gaps:
        row["data_gaps"] = gaps
    return row


def analyze_stocks(tickers) -> dict:
    """Full deep battery on a basket of stocks in ONE call (max 20 tickers).

    For each ticker this runs, server-side and concurrently, everything the
    per-name evaluation battery used to take ~10 separate calls to gather:
    market snapshot, valuation on all three lenses (vs own 5-yr history with a
    LIVE current point, vs peers, reverse-DCF implied growth on a
    distortion-checked FCF base), quality + sector-aware distress composite,
    multi-year financial trends and lifecycle stage, risk vs SPY, momentum,
    dividend safety, earnings-date proximity, ownership/short interest, and
    the top company-specific headlines. ETFs in the list get a fund-shaped
    scorecard (AUM, expense ratio, holdings) instead of erroring.

    Every number is computed from one canonical basis set (live price; labeled
    EPS/FCF bases), so figures here match the single-stock tools exactly. The
    "flags" list per name carries the deterministic gotcha checks (distorted
    FCF base, transitory-earnings risk, Z-score sector artifacts, dilution,
    earnings event risk...). Facts only — ranking and judgment are yours.

    Use screen_stocks first to narrow a wide field cheaply; use this on the
    shortlist. `tickers` may be a list or comma/space-separated string.
    """
    if isinstance(tickers, str):
        tickers = [t for t in tickers.replace(",", " ").split() if t]
    seen: list[str] = []
    for t in tickers:
        nt = normalize_ticker(t)
        if nt and nt not in seen:
            seen.append(nt)
    if not seen:
        return {"error": "no tickers provided"}
    if len(seen) > _MAX_FULL:
        return {"error": f"max {_MAX_FULL} tickers per analyze_stocks call "
                         f"(got {len(seen)}) — triage with screen_stocks first, "
                         f"then deep-dive the shortlist"}

    results = parallel_map(_analyze_one, seen, workers=8)
    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    basket = _basket_relative(ok)
    _archive_rows(ok)
    return {
        "count": len(ok),
        "results": ok,
        "basket_medians": basket,
        "failed": failed or None,
        "archived": "scorecards appended to data/snapshots/ (point-in-time "
                    "record; retrieve with read_snapshot)",
        "note": "objective scorecards only — apply judgment (and WebSearch for "
                "forward catalysts: backlog, guidance, contracts) before ranking",
    }


def _basket_relative(rows: list[dict]) -> dict | None:
    """Medians across the stocks in this call + per-row premium/discount.

    For thematic baskets, FMP's sector-classification peers are often nonsense
    (a quantum run got Bilibili and Duolingo as 'peers'). The basket the
    caller assembled IS the peer group — so compare against it.
    """
    stocks = [r for r in rows if r.get("instrument") == "stock"]
    if len(stocks) < 3:
        return None
    import statistics

    def med(vals, positive_only=False):
        vals = [v for v in vals if isinstance(v, (int, float))]
        if positive_only:
            vals = [v for v in vals if v > 0]
        return round(statistics.median(vals), 2) if vals else None

    basket = {
        "pe_forward": med([r["valuation"]["pe_forward"] for r in stocks],
                          positive_only=True),
        "ps_ttm": med([r["valuation"]["ps_ttm"] for r in stocks],
                      positive_only=True),
        "fcf_yield_ttm_pct": med([r["valuation"]["fcf_yield_ttm_pct"]
                                  for r in stocks]),
        "pct_off_52w_high": med([r["pct_off_52w_high"] for r in stocks]),
        "perf_1y_pct": med([(r.get("momentum") or {}).get("perf_1y_pct")
                            for r in stocks]),
        "ann_vol_pct": med([(r.get("risk") or {}).get("ann_vol_pct")
                            for r in stocks]),
        "note": "medians across the stocks in THIS call — for thematic baskets "
                "this peer group usually beats FMP's sector-classification peers",
    }
    for r in stocks:
        rel = {}
        ps, bps = r["valuation"]["ps_ttm"], basket["ps_ttm"]
        if ps and ps > 0 and bps:
            rel["ps_premium_vs_basket_pct"] = round((ps / bps - 1) * 100, 1)
        pf, bpf = r["valuation"]["pe_forward"], basket["pe_forward"]
        if pf and pf > 0 and bpf:
            rel["pe_fwd_premium_vs_basket_pct"] = round((pf / bpf - 1) * 100, 1)
        if rel:
            r["valuation"]["vs_basket"] = rel
    return basket


# ---------------------------------------------------------------------------
# Point-in-time archive. Every battery scorecard is appended to a per-ticker
# JSONL so the decision-review loop can ask "what did we KNOW on the day we
# decided?" — separating data-was-wrong from judgment-was-wrong. Unlike the
# TTL cache, this never evicts.
# ---------------------------------------------------------------------------

def _snapshot_dir():
    d = data_dir() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_rows(rows: list[dict]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    today = ts[:10]
    for r in rows:
        ticker = r.get("ticker")
        if not ticker:
            continue
        path = _snapshot_dir() / f"{ticker}.jsonl"
        try:
            # One snapshot per ticker per day — a warm-cache re-run minutes
            # later adds nothing but noise.
            if path.exists():
                last = path.read_text(encoding="utf-8").rstrip().rsplit("\n", 1)[-1]
                if last and json.loads(last).get("as_of", "")[:10] == today:
                    continue
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"as_of": ts, "scorecard": r}) + "\n")
        except (OSError, ValueError):
            pass  # archival must never break the analysis itself


def read_snapshot(ticker: str, date: str = None) -> dict:
    """What did we know about a ticker on a given date? (point-in-time recall)

    Every analyze_stocks run archives its scorecards. This returns the
    archived scorecard as of `date` (YYYY-MM-DD — the latest snapshot at or
    before it; omit for the most recent), plus the list of available snapshot
    dates. Use it when reviewing a past decision: compare what the data said
    then against what happened since, so a bad outcome can be attributed to
    bad data, bad judgment, or bad luck.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    path = _snapshot_dir() / f"{ticker}.jsonl"
    if not path.exists():
        return {"error": f"no archived snapshots for {ticker} — it has never "
                         f"been through analyze_stocks", "ticker": ticker}

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if not entries:
        return {"error": f"snapshot file for {ticker} is empty/corrupt",
                "ticker": ticker}

    dates = [e.get("as_of", "")[:10] for e in entries]
    if date:
        eligible = [e for e in entries if e.get("as_of", "")[:10] <= str(date)]
        if not eligible:
            return {"error": f"no snapshot at or before {date} — earliest is "
                             f"{dates[0]}", "ticker": ticker,
                    "available_dates": dates}
        chosen = eligible[-1]
    else:
        chosen = entries[-1]

    return {
        "ticker": ticker,
        "as_of": chosen.get("as_of"),
        "available_dates": dates,
        "scorecard": chosen.get("scorecard"),
    }
