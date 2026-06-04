"""Persistence: saved evaluations + decision journal. All paths absolute."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from .common import data_dir, normalize_ticker

_VALID_DECISIONS = {"buy", "sell", "hold", "pass", "wait"}


def _reports_dir():
    d = data_dir() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decisions_path():
    return data_dir() / "decisions.jsonl"


def _plus_six_months(d: date) -> date:
    month = d.month - 1 + 6
    year = d.year + month // 12
    month = month % 12 + 1
    # Clamp day to the last valid day of the target month.
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                      else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def save_evaluation(ticker: str, content: str) -> dict:
    """Save a markdown stock evaluation to disk.

    Writes `content` to reports/{TICKER}_{YYYY-MM-DD}.md (today's date) under the
    server's data directory. If an evaluation already exists for the ticker today
    it is overwritten. Returns the absolute path written.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not content or not content.strip():
        return {"error": "refusing to save empty evaluation content"}

    today = date.today().isoformat()
    path = _reports_dir() / f"{ticker}_{today}.md"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"error": f"could not write evaluation: {exc}"}
    return {"ticker": ticker, "date": today, "path": str(path)}


def get_evaluation_history(ticker: str) -> dict:
    """List previously saved evaluations for a ticker, newest first.

    Returns each saved evaluation's date and absolute path. Use read_evaluation
    to fetch the content of a specific one.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    entries = []
    for path in _reports_dir().glob(f"{ticker}_*.md"):
        stem_date = path.stem[len(ticker) + 1:]  # after "{TICKER}_"
        entries.append({"date": stem_date, "path": str(path)})
    entries.sort(key=lambda e: e["date"], reverse=True)
    return {"ticker": ticker, "count": len(entries), "evaluations": entries}


def read_evaluation(ticker: str, date: str) -> dict:
    """Return the content of a specific saved evaluation.

    `date` must be in YYYY-MM-DD form, matching a date from
    get_evaluation_history. Returns the markdown content, or an error if no
    evaluation exists for that ticker/date.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    path = _reports_dir() / f"{ticker}_{date}.md"
    if not path.exists():
        return {"error": f"no evaluation found for {ticker} on {date}",
                "ticker": ticker, "date": date}
    try:
        return {"ticker": ticker, "date": date, "path": str(path),
                "content": path.read_text(encoding="utf-8")}
    except OSError as exc:
        return {"error": f"could not read evaluation: {exc}"}


def save_decision(ticker: str, decision: str, rationale: str) -> dict:
    """Append a decision to the decision journal (decisions.jsonl).

    `decision` must be one of: buy, sell, hold, pass, wait. Records a UTC
    timestamp, the ticker, the decision, the rationale, and a review_after date
    (six months out) for revisiting the call. Returns the stored entry.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    decision = (decision or "").strip().lower()
    if decision not in _VALID_DECISIONS:
        return {"error": f"decision must be one of {sorted(_VALID_DECISIONS)}, "
                         f"got '{decision}'"}
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}

    now = datetime.now(timezone.utc)
    entry = {
        "timestamp": now.isoformat(),
        "ticker": ticker,
        "decision": decision,
        "rationale": rationale.strip(),
        "review_after": _plus_six_months(now.date()).isoformat(),
    }
    try:
        with _decisions_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        return {"error": f"could not append decision: {exc}"}
    return {"saved": True, "path": str(_decisions_path()), "entry": entry}


def query_decisions(ticker: str | None = None) -> dict:
    """Return decision-journal entries, optionally filtered by ticker.

    Reads decisions.jsonl and returns entries newest first. Pass a ticker to
    filter to one stock, or omit it to get the full journal.
    """
    path = _decisions_path()
    if not path.exists():
        return {"count": 0, "decisions": []}

    wanted = normalize_ticker(ticker) if ticker else None
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip a corrupt line rather than failing the query
            if wanted and obj.get("ticker") != wanted:
                continue
            entries.append(obj)
    except OSError as exc:
        return {"error": f"could not read decision journal: {exc}"}

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return {"ticker": wanted, "count": len(entries), "decisions": entries}
