"""Portfolio layer: real holdings as a first-class object.

The user's actual book previously entered analysis as a screenshot. Holdings
live in data/holdings.json (set_holdings); portfolio_report turns them into
the risk picture a sector scorecard can't show: weights and concentration,
weighted volatility/beta, a distress census, the earnings calendar across
positions, and coverage gaps — positions with no journaled decision and no
tripwire watching them. Facts and arithmetic only; rebalancing is judgment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .common import data_dir, normalize_ticker, num, parallel_map


def _holdings_path():
    return data_dir() / "holdings.json"


def set_holdings(positions) -> dict:
    """Store the real portfolio. `positions` maps ticker -> share quantity,
    e.g. {"CEG": 0.69, "BWXT": 0.84} (a JSON string is also accepted).
    Replaces the previous holdings file; an empty dict clears it. Fractional
    shares are fine. Cash can be recorded under the ticker "CASH" with a
    dollar amount as the quantity.
    """
    if isinstance(positions, str):
        try:
            positions = json.loads(positions)
        except ValueError:
            return {"error": "positions must be a JSON object: "
                             "{\"TICKER\": quantity, ...}"}
    if not isinstance(positions, dict):
        return {"error": "positions must map tickers to share quantities"}

    clean: dict[str, float] = {}
    for t, qty in positions.items():
        nt = normalize_ticker(str(t))
        q = num(qty)
        if nt and q is not None and q > 0:
            clean[nt] = q
    payload = {"updated": datetime.now(timezone.utc).isoformat(),
               "positions": clean}
    try:
        _holdings_path().write_text(json.dumps(payload, indent=1),
                                    encoding="utf-8")
    except OSError as exc:
        return {"error": f"could not write holdings: {exc}"}
    return {"saved": True, "count": len(clean), "positions": clean}


def portfolio_report() -> dict:
    """The whole book in one call: weights, concentration, risk, coverage.

    Values each position at the live price, then reports: per-position weight
    and value; sector/theme concentration; value-weighted volatility and
    realized beta; a distress census (which holdings carry watch/elevated/
    acute flags); earnings dates within 14 days; the highest-signal flags per
    name; and coverage gaps — positions with no journaled decision or no
    tripwire watching them. Every position's full scorecard is archived as a
    snapshot. Uses set_holdings data; run that first.
    """
    p = _holdings_path()
    if not p.exists():
        return {"error": "no holdings stored — call set_holdings first"}
    try:
        stored = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"could not read holdings: {exc}"}
    positions: dict = stored.get("positions") or {}
    cash = num(positions.get("CASH")) or 0.0
    tickers = [t for t in positions if t != "CASH"]
    if not tickers and not cash:
        return {"error": "holdings file is empty — call set_holdings first"}

    from .battery import _analyze_one, _compact_row, _archive_rows
    rows_full = parallel_map(_analyze_one, tickers, workers=8)
    _archive_rows([r for r in rows_full if isinstance(r, dict) and "error" not in r])

    holdings, failed = [], []
    for t, r in zip(tickers, rows_full):
        if not isinstance(r, dict) or "error" in r or r.get("price") is None:
            failed.append({"ticker": t,
                           "error": (r or {}).get("error", "no data")})
            continue
        qty = num(positions[t]) or 0.0
        holdings.append({"row": _compact_row(r), "qty": qty,
                         "value": round(qty * r["price"], 2)})

    equity_value = sum(h["value"] for h in holdings)
    total_value = equity_value + cash
    if total_value <= 0:
        return {"error": "portfolio value computed as zero", "failed": failed}

    # Journal + tripwire coverage.
    journaled = set()
    dec_path = data_dir() / "decisions.jsonl"
    if dec_path.exists():
        try:
            for line in dec_path.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line) if line.strip() else {}
                if obj.get("ticker"):
                    journaled.add(obj["ticker"])
        except (OSError, ValueError):
            pass
    from .watchtower import _load_tripwires
    wired = {t.get("ticker") for t in _load_tripwires()}

    per_position = []
    sectors: dict[str, float] = {}
    w_vol = w_beta = vol_weight_sum = beta_weight_sum = 0.0
    distress_census: dict[str, list[str]] = {}
    earnings_soon = []
    for h in sorted(holdings, key=lambda x: x["value"], reverse=True):
        r, w = h["row"], h["value"] / total_value
        sector = r.get("sector") or "Unknown"
        sectors[sector] = sectors.get(sector, 0.0) + w
        vol = (r.get("risk") or {}).get("ann_vol_pct")
        beta = (r.get("risk") or {}).get("realized_beta")
        if vol is not None:
            w_vol += w * vol
            vol_weight_sum += w
        if beta is not None:
            w_beta += w * beta
            beta_weight_sum += w
        sev = (r.get("quality") or {}).get("distress_severity")
        if sev and sev != "none":
            distress_census.setdefault(sev, []).append(r["ticker"])
        ne = r.get("next_earnings") or {}
        if ne.get("days_until") is not None and ne["days_until"] <= 14:
            earnings_soon.append({"ticker": r["ticker"],
                                  "date": ne.get("date"),
                                  "days": ne["days_until"]})
        per_position.append({
            "ticker": r["ticker"],
            "qty": h["qty"],
            "price": r.get("price"),
            "value": h["value"],
            "weight_pct": round(w * 100, 1),
            "sector": sector,
            "distress": sev,
            "next_earnings_days": ne.get("days_until"),
            "journaled": r["ticker"] in journaled,
            "tripwired": r["ticker"] in wired,
            "top_flags": (r.get("flags") or [])[:3],
        })

    weights = [pp["weight_pct"] for pp in per_position]
    coverage_gaps = {
        "unjournaled": [pp["ticker"] for pp in per_position
                        if not pp["journaled"]],
        "no_tripwires": [pp["ticker"] for pp in per_position
                         if not pp["tripwired"]],
    }
    notes = []
    if weights and max(weights) > 18:
        notes.append(f"largest position is {max(weights):.0f}% — above the "
                     f"~15-18% single-name concentration guideline")
    top_sector, top_w = (max(sectors.items(), key=lambda kv: kv[1])
                         if sectors else (None, 0))
    if top_w > 0.5:
        notes.append(f"{top_sector} is {top_w * 100:.0f}% of the book — "
                     f"single-theme concentration; positions will move together")
    if cash / total_value < 0.05:
        notes.append("cash under 5% — no dry powder for pre-committed "
                     "dip triggers")

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "total_value": round(total_value, 2),
        "cash": round(cash, 2),
        "cash_pct": round(cash / total_value * 100, 1),
        "positions": per_position,
        "sector_weights_pct": {k: round(v * 100, 1)
                               for k, v in sorted(sectors.items(),
                                                  key=lambda kv: -kv[1])},
        "weighted_volatility_pct": (round(w_vol / vol_weight_sum, 1)
                                    if vol_weight_sum else None),
        "weighted_beta": (round(w_beta / beta_weight_sum, 2)
                          if beta_weight_sum else None),
        "distress_census": distress_census or None,
        "earnings_within_14d": earnings_soon or None,
        "coverage_gaps": coverage_gaps,
        "notes": notes,
        "failed": failed or None,
        "note": "facts and arithmetic only — rebalancing is judgment; "
                "scorecards archived to snapshots",
    }
