"""Value/quality autopilot sleeve (Sleeve A) — PAPER only.

Flow: scan the universe -> build a confidence-weighted top-N target portfolio ->
diff against current paper holdings -> place simulated orders to reach the
targets -> LOG every trade with its full point-in-time rationale.

The logging is the point. Each trade snapshots the metrics that drove it, so a
later session can mine `autopilot_log.jsonl` for "which signals actually
predicted returns" and improve the rules/tools. Profit is not the goal; the
feedback dataset is.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from .alpaca import paper_account, paper_place_order, paper_positions
from .common import data_dir
from .screen import scan_universe

_LOG = "autopilot_log.jsonl"
_MIN_TRADE_USD = 50.0     # don't bother with trades smaller than this


def _log_path():
    return data_dir() / _LOG


def _snapshot(p: dict) -> dict:
    """The metric snapshot we log per trade — the raw material for learning."""
    return {k: p.get(k) for k in (
        "forward_pe", "trailing_pe", "price_to_sales", "price_to_book",
        "fcf_yield_pct", "profit_margin_pct", "revenue_growth_pct",
        "dividend_pct", "beta", "pct_off_52w_high", "analyst_upside_pct",
        "score", "verdict", "target_weight_pct")}


# Per-sleeve scan parameters and the default capital split across sleeves.
_SLEEVE_PARAMS = {
    "value":  {"top_n": 100, "max_weight": 0.10},
    "growth": {"top_n": 25,  "max_weight": 0.15},
    "ipo":    {"top_n": 15,  "max_weight": 0.15},
}
_DEFAULT_ALLOC = {"value": 0.60, "growth": 0.25, "ipo": 0.15}


def autopilot_run(dry_run: bool = True, allocations: dict | None = None,
                  power: float = 3.5) -> dict:
    """Run the multi-sleeve autopilot once (PAPER).

    Allocates capital across three sleeves and rebalances the paper account
    toward the combined confidence-weighted target, logging every trade with its
    full rationale + sleeve tag.

    Sleeves (default split): "value" 60% (S&P 500 cheap-quality), "growth" 25%
    (thematic/growth names — quantum, nuclear, AI, fintech…), "ipo" 15% (recent
    IPOs). Override with `allocations`, e.g. {"value":0.5,"growth":0.3,"ipo":0.2}.

    dry_run=True (default) PREVIEWS the plan without trading; dry_run=False
    executes on the paper account. Returns the plan (buys/sells/exits per sleeve).
    """
    acct = paper_account()
    if "error" in acct:
        return {"error": f"paper account unavailable: {acct['error']}"}
    equity = acct.get("equity")
    if not equity:
        return {"error": "could not read paper account equity"}

    alloc = allocations or dict(_DEFAULT_ALLOC)
    pos = paper_positions()
    held = {p["symbol"]: p["qty"] for p in pos.get("positions", [])} if "error" not in pos else {}

    # Build the combined target book across sleeves.
    targets: dict[str, dict] = {}
    sleeve_summary = {}
    for sleeve, frac in alloc.items():
        if not frac or frac <= 0:
            continue
        params = _SLEEVE_PARAMS.get(sleeve, {"top_n": 25, "max_weight": 0.15})
        scan = scan_universe(sleeve=sleeve, top_n=params["top_n"],
                             max_weight=params["max_weight"], power=power)
        sleeve_summary[sleeve] = {"allocation_pct": round(frac * 100, 1),
                                  "scanned": scan.get("scanned"),
                                  "selected": scan.get("selected")}
        for p in scan.get("picks", []):
            px = p.get("price")
            w = (p.get("target_weight_pct") or 0) / 100   # within-sleeve weight
            if not px or w <= 0:
                continue
            port_w = w * frac                              # portfolio weight
            shares = math.floor(equity * port_w / px)
            if shares < 1:
                continue
            tk = p["ticker"]
            if tk in targets:                              # name in >1 sleeve: combine
                targets[tk]["shares"] += shares
            else:
                snap = _snapshot(p)
                snap["sleeve"] = sleeve
                targets[tk] = {"shares": shares, "price": px, "snapshot": snap,
                               "sleeve": sleeve}

    if not targets:
        return {"error": "no targets produced", "sleeves": sleeve_summary}

    buys, sells, exits = [], [], []
    for tk, t in targets.items():
        delta = t["shares"] - held.get(tk, 0)
        if abs(delta) * t["price"] < _MIN_TRADE_USD:
            continue
        side = "buy" if delta > 0 else "sell"
        rec = {"ticker": tk, "side": side, "shares": abs(delta), "price": t["price"],
               "sleeve": t["sleeve"], "snapshot": t["snapshot"]}
        (buys if side == "buy" else sells).append(rec)
    for sym, qty in held.items():
        if sym not in targets and qty and qty > 0:
            exits.append({"ticker": sym, "side": "sell", "shares": qty,
                          "sleeve": "exit", "reason": "no longer a sleeve target"})

    plan = {
        "mode": "DRY RUN (preview, no orders)" if dry_run else "EXECUTED (paper)",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "equity": equity,
        "sleeves": sleeve_summary,
        "targets": len(targets),
        "buys": len(buys), "sells": len(sells), "exits": len(exits),
        "buy_list": [{"ticker": b["ticker"], "sleeve": b["sleeve"],
                      "weight_pct": b["snapshot"].get("target_weight_pct"),
                      "verdict": b["snapshot"].get("verdict")} for b in buys],
        "exit_list": [e["ticker"] for e in exits],
    }
    if dry_run:
        plan["note"] = "preview only — call autopilot_run(dry_run=False) to execute"
        return plan

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    executed, errors = 0, []
    with _log_path().open("a", encoding="utf-8") as f:
        for rec in buys + sells + exits:
            o = paper_place_order(rec["ticker"], rec["shares"], rec["side"])
            if "error" in o:
                errors.append({"ticker": rec["ticker"], "error": o["error"]})
                continue
            executed += 1
            f.write(json.dumps({
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sleeve": rec.get("sleeve", "value"),
                "ticker": rec["ticker"], "action": rec["side"],
                "shares": rec["shares"], "price": rec.get("price"),
                "order_status": o.get("status"),
                "reason": rec.get("reason", f"{rec.get('sleeve')} sleeve target"),
                "metrics": rec.get("snapshot", {}),
            }) + "\n")

    plan["executed_orders"] = executed
    plan["errors"] = errors
    plan["log"] = str(_log_path())
    return plan


def review_autopilot() -> dict:
    """Mine the autopilot log + paper P&L for what's working (the learning loop).

    Joins every logged trade with its current paper position to attach an
    unrealized return, then breaks performance down by the signals that drove
    the buy — confidence-score quintile, FCF-yield tier, and verdict — and
    compares the portfolio's total return to the S&P 500 over the same window.
    This is the feedback that tells us which signals actually predict returns,
    so the rules and tools can be improved. (Needs weeks of data to be
    meaningful; early on the returns are ~flat.)
    """
    import json as _json
    import statistics

    log = _log_path()
    if not log.exists():
        return {"error": "no autopilot log yet — run autopilot_run first"}
    entries = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue

    acct = paper_account()
    pos = paper_positions()
    held = {p["symbol"]: p for p in pos.get("positions", [])} if "error" not in pos else {}

    # Attach current unrealized return to each BUY still held.
    buys = [e for e in entries if e.get("action") == "buy" and e["ticker"] in held]
    for e in buys:
        e["current_return_pct"] = held[e["ticker"]].get("unrealized_pl_pct")

    def bucket_returns(key_fn, labels):
        out = {}
        for e in buys:
            r = e.get("current_return_pct")
            if r is None:
                continue
            out.setdefault(key_fn(e), []).append(r)
        return {labels.get(k, k): {"n": len(v), "avg_return_pct": round(statistics.mean(v), 2)}
                for k, v in out.items()}

    # Score quintile (within this dataset).
    scores = sorted(e["metrics"].get("score", 0) for e in buys)
    def quintile(e):
        s = e["metrics"].get("score", 0)
        if not scores:
            return "?"
        idx = sum(1 for x in scores if x <= s) / len(scores)
        return "top20%" if idx > 0.8 else "Q2" if idx > 0.6 else "Q3" if idx > 0.4 else "Q4" if idx > 0.2 else "bottom20%"

    # Benchmark: S&P 500 (SPY) over the window since the first trade.
    bench = None
    try:
        import warnings
        import yfinance as yf
        warnings.filterwarnings("ignore")
        first = min(e["timestamp"][:10] for e in entries)
        spy = yf.Ticker("SPY").history(start=first)["Close"].dropna()
        if len(spy) >= 2:
            bench = round((spy.iloc[-1] / spy.iloc[0] - 1) * 100, 2)
    except Exception:
        pass

    equity = acct.get("equity")
    total_return = (round((equity / 100000 - 1) * 100, 2)
                    if equity else None)
    return {
        "trades_logged": len(entries),
        "positions_held": len(held),
        "portfolio_total_return_pct": total_return,
        "sp500_return_same_window_pct": bench,
        "vs_sp500_pct": round(total_return - bench, 2)
        if (total_return is not None and bench is not None) else None,
        "return_by_score_quintile": bucket_returns(quintile, {}),
        "return_by_verdict": bucket_returns(
            lambda e: e["metrics"].get("verdict", "?"), {}),
        "note": "needs weeks of data to be meaningful; use it to find which "
                "signals predict returns, then refine the rules/tools.",
    }
