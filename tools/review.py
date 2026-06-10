"""Decision-review machinery: assemble the grading dossier, keep judgment human.

Every journaled decision carries a 6-month review date and a link to the
point-in-time scorecard from decision day. Grading a call honestly needs
both sides laid out: what was known THEN, what is true NOW, and what the
market did in between (absolute and vs SPY — being up 10% while SPY rose 25%
is not a win). These tools assemble that dossier in one call; whether the
call was right, wrong-on-data, or wrong-on-judgment stays with the reader.
"""

from __future__ import annotations

import json
from datetime import date

from .common import data_dir, get_history, normalize_ticker, num


def _decisions() -> list[dict]:
    p = data_dir() / "decisions.jsonl"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def pending_reviews(within_days: int = 30) -> dict:
    """Journal decisions due (or coming due) for their scheduled review.

    Returns the latest decision per ticker whose review_after date is past,
    plus those falling due within `within_days`. Pair each with
    review_dossier(ticker) to do the actual grading.
    """
    latest: dict[str, dict] = {}
    for obj in _decisions():
        if obj.get("ticker"):
            latest[obj["ticker"]] = obj  # file is chronological; newest wins

    today = date.today()
    due, upcoming = [], []
    for obj in latest.values():
        ra = str(obj.get("review_after") or "")
        if not ra:
            continue
        try:
            days = (date.fromisoformat(ra) - today).days
        except ValueError:
            continue
        item = {"ticker": obj["ticker"], "decision": obj.get("decision"),
                "decided": str(obj.get("timestamp", ""))[:10],
                "review_after": ra,
                "rationale_head": str(obj.get("rationale", ""))[:120]}
        if days <= 0:
            due.append(item)
        elif days <= within_days:
            item["due_in_days"] = days
            upcoming.append(item)

    due.sort(key=lambda x: x["review_after"])
    upcoming.sort(key=lambda x: x["review_after"])
    return {"as_of": today.isoformat(), "due": due, "upcoming": upcoming,
            "journal_size": len(latest)}


def _pct_change_since(ticker: str, start_iso: str) -> float | None:
    """Price change % from the first close on/after start date to the latest."""
    hist = get_history(ticker, period="1y")
    if hist is None or hist.empty or "Close" not in hist:
        return None
    closes = hist["Close"].dropna()
    try:
        window = closes[closes.index >= start_iso]
    except TypeError:
        return None
    if window.empty or window.iloc[0] <= 0:
        return None
    return round(float(window.iloc[-1] / window.iloc[0] - 1) * 100, 1)


def review_dossier(ticker: str) -> dict:
    """Everything needed to grade a journaled call, in one payload.

    Returns: every journal entry for the ticker (newest first); the archived
    scorecard from decision day (what was known THEN); a fresh compact
    scorecard (what is true NOW); price change since the decision, absolute
    AND vs SPY over the same window; and the ticker's tripwires + fired-alert
    history. The grading question to answer from it: was the outcome driven
    by the data being wrong, the judgment being wrong, or luck — and what
    does that teach the next call? Verdicts stay with the reviewer.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    entries = [d for d in _decisions() if d.get("ticker") == ticker]
    if not entries:
        return {"error": f"no journaled decisions for {ticker}", "ticker": ticker}
    entries.reverse()  # newest first
    latest = entries[0]
    decided_date = (latest.get("snapshot_as_of")
                    or latest.get("timestamp", ""))[:10]

    # What was known then.
    from .battery import read_snapshot
    then = read_snapshot(ticker, date=decided_date)
    then_card = then.get("scorecard") if "error" not in then else None

    # What is true now (compact battery row; archives today's snapshot too).
    from .battery import _analyze_one, _compact_row
    try:
        now_card = _compact_row(_analyze_one(ticker))
    except Exception as exc:  # noqa: BLE001
        now_card = {"error": f"current scorecard failed: {exc}"}

    # Market context: the call's window, absolute and vs SPY.
    px_chg = _pct_change_since(ticker, decided_date)
    spy_chg = _pct_change_since("SPY", decided_date)
    performance = {
        "since": decided_date,
        "price_change_pct": px_chg,
        "spy_change_pct": spy_chg,
        "relative_to_spy_pct": (round(px_chg - spy_chg, 1)
                                if (px_chg is not None and spy_chg is not None)
                                else None),
    }

    # Standing tripwires + what has fired.
    from .watchtower import _alerts_path, _load_tripwires
    tripwires = [t for t in _load_tripwires() if t.get("ticker") == ticker]
    fired = []
    ap = _alerts_path()
    if ap.exists():
        try:
            for line in ap.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line) if line.strip() else {}
                if obj.get("ticker") == ticker:
                    fired.append(obj)
        except (OSError, ValueError):
            pass

    return {
        "ticker": ticker,
        "decisions": entries,
        "decided_date": decided_date,
        "then": then_card or {"note": "no archived snapshot from decision day"},
        "now": now_card,
        "performance": performance,
        "tripwires": tripwires,
        "alerts_fired": fired,
        "grading_guide": "right / wrong-on-data (inputs were bad) / "
                         "wrong-on-judgment (inputs were fine, the read wasn't) "
                         "/ luck (outcome unrelated to thesis) — and one lesson",
    }
