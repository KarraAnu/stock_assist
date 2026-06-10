"""Watchtower: tripwire monitoring for journaled decisions.

Decisions in the journal carry pre-committed triggers ("add on -25% with RPO
intact", "reduce if revisions flip negative") — but pre-commitments only
discipline you if something actually watches them. The watchtower stores
machine-checkable tripwires, evaluates them against live data on demand (or
on a schedule), fires alerts with the pre-committed action attached, and
lists decision reviews that have come due.

Facts only, as ever: a fired tripwire reports the condition and the note you
wrote when you were calm — the action remains yours.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from .common import data_dir, normalize_ticker, num, parallel_map

_METRICS = ("price", "pct_change_since_decision_pct", "fwd_pe",
            "pct_above_sma200", "days_to_earnings", "eps_revision_90d_pct",
            "target_momentum_pct", "rpo_latest_usd_m", "rpo_yoy_pct",
            "short_pct_of_float", "interest_coverage")
_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def _tripwires_path():
    return data_dir() / "tripwires.json"


def _alerts_path():
    return data_dir() / "alerts.jsonl"


def _load_tripwires() -> list[dict]:
    p = _tripwires_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_tripwires(tw: list[dict]) -> None:
    _tripwires_path().write_text(json.dumps(tw, indent=1), encoding="utf-8")


def add_tripwire(ticker: str, metric: str, op: str, value: float,
                 note: str, cooldown_days: int = 5) -> dict:
    """Register a machine-checkable tripwire for the watchtower.

    `metric` is one of: price, pct_change_since_decision_pct, fwd_pe,
    pct_above_sma200, days_to_earnings, eps_revision_90d_pct,
    target_momentum_pct, rpo_latest_usd_m, rpo_yoy_pct, short_pct_of_float,
    interest_coverage. `op` is one of < <= > >=. `note` should state the
    PRE-COMMITTED ACTION, written now while calm (e.g. "add to 10% from
    cash — thesis intact if RPO held"). After firing, a tripwire stays
    quiet for `cooldown_days` before it can fire again.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if metric not in _METRICS:
        return {"error": f"unknown metric '{metric}' — use one of {_METRICS}"}
    if op not in _OPS:
        return {"error": f"op must be one of {sorted(_OPS)}"}
    if not note or not note.strip():
        return {"error": "note (the pre-committed action) is required"}
    v = num(value)
    if v is None:
        return {"error": "value must be a number"}

    tw = _load_tripwires()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "ticker": ticker,
        "metric": metric,
        "op": op,
        "value": v,
        "note": note.strip(),
        "cooldown_days": max(0, int(cooldown_days)),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    tw.append(entry)
    _save_tripwires(tw)
    return {"saved": True, "tripwire": entry, "total_active": len(tw)}


def remove_tripwire(tripwire_id: str) -> dict:
    """Remove a tripwire by id (ids come from list_tripwires/add_tripwire)."""
    tw = _load_tripwires()
    kept = [t for t in tw if t.get("id") != tripwire_id]
    if len(kept) == len(tw):
        return {"error": f"no tripwire with id {tripwire_id}"}
    _save_tripwires(kept)
    return {"removed": tripwire_id, "remaining": len(kept)}


def list_tripwires() -> dict:
    """All registered tripwires plus the recent alert history."""
    alerts = []
    p = _alerts_path()
    if p.exists():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
            alerts = [json.loads(x) for x in lines[-10:] if x.strip()]
        except (OSError, ValueError):
            pass
    return {"tripwires": _load_tripwires(), "recent_alerts": alerts}


def _decision_price(ticker: str) -> float | None:
    """Price from the snapshot linked to the ticker's most recent decision.

    Older decisions predate the snapshot linkage — fall back to the latest
    archived snapshot at or before the decision date.
    """
    dec_path = data_dir() / "decisions.jsonl"
    snap_date = None
    if dec_path.exists():
        try:
            for line in dec_path.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line) if line.strip() else {}
                if obj.get("ticker") == ticker:
                    snap_date = (obj.get("snapshot_as_of")
                                 or obj.get("timestamp", ""))[:10]
        except (OSError, ValueError):
            pass
    if not snap_date:
        return None
    from .battery import read_snapshot
    snap = read_snapshot(ticker, date=snap_date)
    if "error" in snap:
        return None
    return num((snap.get("scorecard") or {}).get("price"))


def _metrics_for(ticker: str, needed: set[str]) -> dict:
    """Fetch only the metrics this ticker's tripwires reference."""
    out: dict = {}
    if needed & {"price", "fwd_pe", "short_pct_of_float",
                 "pct_change_since_decision_pct"}:
        from .market import get_market_data
        md = get_market_data(ticker)
        if "error" not in md:
            out["price"] = md.get("price")
            out["fwd_pe"] = num((md.get("valuation") or {}).get("forward_pe"))
    if "pct_change_since_decision_pct" in needed and out.get("price"):
        base = _decision_price(ticker)
        if base:
            out["pct_change_since_decision_pct"] = round(
                (out["price"] / base - 1) * 100, 1)
    if "short_pct_of_float" in needed:
        from .ownership import get_ownership
        own = get_ownership(ticker)
        out["short_pct_of_float"] = (own.get("short_interest") or {}).get(
            "short_pct_of_float")
    if "pct_above_sma200" in needed:
        from .indicators import get_indicators
        ind = get_indicators(ticker, period="2y")
        if "error" not in ind:
            out["pct_above_sma200"] = ind.get("pct_above_sma200")
    if "days_to_earnings" in needed:
        from .market import get_earnings_calendar
        ec = get_earnings_calendar(ticker)
        out["days_to_earnings"] = ec.get("days_until")
    if needed & {"eps_revision_90d_pct", "target_momentum_pct"}:
        from .estimates import get_estimates
        est = get_estimates(ticker)
        revs = est.get("eps_revisions") or {}
        nfy = revs.get("next_fy") or revs.get("current_fy") or {}
        out["eps_revision_90d_pct"] = nfy.get("revision_90d_pct")
        out["target_momentum_pct"] = (est.get("price_target") or {}).get(
            "momentum_pct")
    if needed & {"rpo_latest_usd_m", "rpo_yoy_pct"}:
        from .filings import get_rpo
        rpo = get_rpo(ticker)
        if rpo.get("available"):
            out["rpo_latest_usd_m"] = (rpo.get("latest") or {}).get("usd_m")
            out["rpo_yoy_pct"] = rpo.get("yoy_growth_pct")
    if "interest_coverage" in needed:
        from .fundamentals import get_quality_scores
        q = get_quality_scores(ticker)
        if "error" not in q:
            out["interest_coverage"] = q.get("interest_coverage")
    return out


def _recent_fires(within_days: int = 30) -> dict[str, str]:
    """tripwire_id -> last fired date (ISO) within the lookback."""
    p = _alerts_path()
    out: dict[str, str] = {}
    if not p.exists():
        return out
    cutoff = date.today().toordinal() - within_days
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line) if line.strip() else {}
            fired = str(obj.get("fired_at", ""))[:10]
            if fired and date.fromisoformat(fired).toordinal() >= cutoff:
                out[obj.get("tripwire_id", "")] = fired
    except (OSError, ValueError):
        pass
    return out


def watchtower_run() -> dict:
    """Evaluate every tripwire against live data; report what fired.

    For each registered tripwire this fetches only the metrics it references,
    checks the condition, and — when it fires — logs an alert (with the
    pre-committed action note) to data/alerts.jsonl. A fired tripwire stays
    quiet for its cooldown period so a standing condition doesn't re-alert
    daily. The report also lists journaled decisions whose 6-month
    review_after date has arrived. Run this on a schedule (or whenever you
    check in); only the "fired" and "reviews_due" sections need attention.
    """
    tw = _load_tripwires()
    by_ticker: dict[str, list[dict]] = {}
    for t in tw:
        by_ticker.setdefault(t["ticker"], []).append(t)

    def check(ticker: str) -> tuple[str, dict]:
        needed = {t["metric"] for t in by_ticker[ticker]}
        return ticker, _metrics_for(ticker, needed)

    results = parallel_map(check, list(by_ticker), workers=6)
    metric_map = {r[0]: r[1] for r in results if isinstance(r, tuple)}
    recent = _recent_fires()
    now = datetime.now(timezone.utc).isoformat()

    fired, watching, no_data = [], [], []
    new_alerts = []
    for t in tw:
        cur = (metric_map.get(t["ticker"]) or {}).get(t["metric"])
        status = {
            "id": t["id"], "ticker": t["ticker"],
            "condition": f"{t['metric']} {t['op']} {t['value']}",
            "current": cur, "note": t["note"],
        }
        if cur is None:
            no_data.append(status)
            continue
        if _OPS[t["op"]](cur, t["value"]):
            last = recent.get(t["id"])
            if last and (date.today() - date.fromisoformat(last)).days < t.get(
                    "cooldown_days", 5):
                status["cooldown_until_after"] = last
                watching.append(status)
            else:
                fired.append(status)
                new_alerts.append({"fired_at": now, "tripwire_id": t["id"],
                                   "ticker": t["ticker"],
                                   "condition": status["condition"],
                                   "current": cur, "note": t["note"]})
        else:
            watching.append(status)

    if new_alerts:
        try:
            with _alerts_path().open("a", encoding="utf-8") as f:
                for a in new_alerts:
                    f.write(json.dumps(a) + "\n")
        except OSError:
            pass

    # Decision reviews that have come due.
    reviews_due = []
    dec_path = data_dir() / "decisions.jsonl"
    if dec_path.exists():
        today = date.today().isoformat()
        latest: dict[str, dict] = {}
        try:
            for line in dec_path.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line) if line.strip() else {}
                if obj.get("ticker"):
                    latest[obj["ticker"]] = obj  # newest per ticker wins
        except (OSError, ValueError):
            pass
        for obj in latest.values():
            if str(obj.get("review_after", "9999")) <= today:
                reviews_due.append({
                    "ticker": obj["ticker"], "decision": obj.get("decision"),
                    "decided": str(obj.get("timestamp", ""))[:10],
                    "review_after": obj.get("review_after"),
                    "snapshot_as_of": obj.get("snapshot_as_of"),
                })

    return {
        "checked_at": now,
        "tripwires_checked": len(tw),
        "fired": fired,
        "reviews_due": reviews_due,
        "watching": watching,
        "no_data": no_data or None,
        "note": "fired tripwires carry the action you pre-committed while "
                "calm — that's the whole point; reviews_due pair with "
                "read_snapshot(ticker, date) to recall decision-time facts",
    }
