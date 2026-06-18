"""Always-on Watchtower: run the tripwire checks and email when something fires.

This is the deterministic monitor that runs WITHOUT any Claude app open —
Windows Task Scheduler (or cron) invokes it on a schedule, it evaluates the
journaled tripwires against live data, and emails only when a tripwire fires
or a decision review comes due. The pre-committed note on each tripwire is the
action you decided on while calm, so the email is self-contained.

No LLM is required: comparing live metrics to thresholds is pure Python. If an
Ollama endpoint is configured (OLLAMA_URL + OLLAMA_MODEL), a short narrative is
prepended to the email as optional enrichment — but the structured alert is
always sent regardless.

Setup (one-time):
  1. Gmail app password: https://myaccount.google.com/apppasswords
  2. Put credentials in a .env file next to this script (gitignored), e.g.:
       FMP_API_KEY=...
       SEC_EDGAR_USER_AGENT=Your Name you@example.com
       SMTP_HOST=smtp.gmail.com
       SMTP_PORT=465
       SMTP_USER=you@gmail.com
       SMTP_PASS=<16-char app password>
       ALERT_TO=you@gmail.com
       # optional local-LLM narrative:
       # OLLAMA_URL=http://localhost:11434
       # OLLAMA_MODEL=llama3.1
  3. Register with Task Scheduler (see README / the schtasks command).

Run manually to test:  python watchtower_cron.py  (add --force to email even
when all-quiet, --dry-run to print instead of send).
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load_dotenv() -> None:
    """Minimal .env loader (no dependency). Lines of KEY=VALUE; # comments."""
    env_path = HERE / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()}  {msg}"
    # Windows consoles default to cp1252 and choke on emoji — encode-safe print.
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    try:
        with (HERE / "data" / "watchtower_cron.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ollama_narrative(report: dict) -> str | None:
    """Optional: a short plain-text summary from a local Ollama model.

    Strictly enrichment — never required, never blocks the alert. The model
    only sees the structured tripwire result; it cannot fetch or analyze
    beyond what's handed to it (that's Claude's job when you open the app).
    """
    url = os.environ.get("OLLAMA_URL")
    model = os.environ.get("OLLAMA_MODEL")
    if not (url and model):
        return None
    try:
        import urllib.request
        fired = report.get("fired") or []
        reviews = report.get("reviews_due") or []
        prompt = (
            "You are a terse portfolio monitor. Below is today's tripwire "
            "result as JSON. In 3-5 sentences, summarize what fired and what "
            "the pre-committed action is for each. Do NOT invent data or give "
            "new advice — only restate the fired conditions and their notes. "
            "End with: 'Open Claude for the full battery + catalyst check.'\n\n"
            + json.dumps({"fired": fired, "reviews_due": reviews})
        )
        payload = json.dumps({"model": model, "prompt": prompt,
                              "stream": False}).encode()
        req = urllib.request.Request(f"{url.rstrip('/')}/api/generate",
                                     data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read()).get("response", "").strip() or None
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the alert
        log(f"ollama enrichment skipped: {exc}")
        return None


def build_body(report: dict, reviews: dict) -> str:
    fired = report.get("fired") or []
    due = report.get("reviews_due") or []
    lines = [f"Watchtower — {datetime.now().strftime('%Y-%m-%d %H:%M')} local",
             f"{report.get('tripwires_checked', 0)} tripwires checked.\n"]

    narrative = ollama_narrative(report)
    if narrative:
        lines += ["── summary ──", narrative, ""]

    if fired:
        lines.append(f"🔴 FIRED ({len(fired)}):")
        for f in fired:
            lines.append(f"  • {f['ticker']}: {f['condition']} "
                         f"(now {f['current']})")
            lines.append(f"      ACTION: {f['note']}")
        lines.append("")
    if due:
        lines.append(f"📅 REVIEWS DUE ({len(due)}):")
        for d in due:
            lines.append(f"  • {d['ticker']} — {d.get('decision')} decided "
                         f"{d.get('decided')}, review was {d.get('review_after')}")
        lines.append("")

    upcoming = (reviews.get("upcoming") or [])
    if upcoming:
        lines.append("Reviews coming within 30 days: "
                     + ", ".join(f"{u['ticker']}({u['review_after']})"
                                 for u in upcoming))
        lines.append("")

    lines.append("Open Claude Code for the full battery + catalyst check on "
                 "anything that fired. (Facts only — not financial advice.)")
    return "\n".join(lines)


def send_email(subject: str, body: str, dry_run: bool = False) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_TO", user)
    from_addr = os.environ.get("ALERT_FROM", user)

    if dry_run or not (user and password and to_addr):
        log(f"[dry-run/no-creds] would send: {subject}\n{body}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx) as server:
        server.login(user, password)
        server.send_message(msg)
    log(f"emailed: {subject} -> {to_addr}")


def main() -> int:
    load_dotenv()
    force = "--force" in sys.argv
    dry_run = "--dry-run" in sys.argv

    try:
        from tools.watchtower import watchtower_run
        from tools.review import pending_reviews
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: could not import watchtower ({exc})")
        return 1

    try:
        report = watchtower_run()
        reviews = pending_reviews()
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: watchtower_run failed ({exc})")
        # A failure to RUN is itself worth an email — silence would be the
        # dangerous failure for a monitor.
        send_email("⚠️ Watchtower ERROR — check the data pipeline",
                   f"watchtower_run raised: {exc}", dry_run=dry_run)
        return 1

    fired = report.get("fired") or []
    due = report.get("reviews_due") or []
    n = report.get("tripwires_checked", 0)

    if fired or due:
        subject = (f"🔴 Watchtower: {len(fired)} fired"
                   + (f", {len(due)} review(s) due" if due else ""))
        send_email(subject, build_body(report, reviews), dry_run=dry_run)
    elif force:
        send_email(f"Watchtower: all quiet ({n} watching)",
                   build_body(report, reviews), dry_run=dry_run)
    else:
        log(f"all quiet ({n} tripwires) — no email")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
