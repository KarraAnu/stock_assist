"""Standalone weekly autopilot runner (for the OS scheduler).

Runs the Sleeve-A paper rebalance without needing Claude Desktop open. Loads the
API keys from the Claude Desktop config (so there's a single source of truth),
runs autopilot_run, and appends a one-line summary to run_history.log.

Schedule it with Windows Task Scheduler (see README / the schtasks command).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"

# Tunables for the scheduled run (multi-sleeve uses the default 60/25/15 split).
POWER = 3.5


def _load_keys() -> None:
    """Populate env from the Claude config's stock-eval env block (if present)."""
    needed = ("FMP_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
              "SEC_EDGAR_USER_AGENT")
    if all(os.environ.get(k) for k in needed):
        return
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        env = cfg["mcpServers"]["stock-eval"]["env"]
        for k in needed:
            if env.get(k) and not os.environ.get(k):
                os.environ[k] = env[k]
    except Exception as exc:
        print(f"warning: could not load keys from config: {exc}", file=sys.stderr)


def main() -> int:
    _load_keys()
    sys.path.insert(0, str(HERE))
    from tools.autopilot import autopilot_run

    started = datetime.now(timezone.utc).isoformat()
    res = autopilot_run(dry_run=False, power=POWER)

    summary = {
        "started": started,
        "mode": res.get("mode"),
        "equity": res.get("equity"),
        "executed_orders": res.get("executed_orders"),
        "buys": res.get("buys"), "sells": res.get("sells"), "exits": res.get("exits"),
        "errors": len(res.get("errors", [])),
        "error": res.get("error"),
    }
    (HERE / "run_history.log").open("a", encoding="utf-8").write(
        json.dumps(summary) + "\n")
    print(json.dumps(summary))
    return 0 if "error" not in res else 1


if __name__ == "__main__":
    raise SystemExit(main())
