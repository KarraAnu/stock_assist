"""Stock Evaluation MCP server.

Exposes objective, deterministic stock-analysis tools to an MCP client (Claude
Desktop / Claude Code). The server returns facts and calculations only — never
recommendations. All judgment (bull/bear/judge synthesis) lives in the
conversation layer; see HANDOFF.md.

Run directly (stdio transport):  python server.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from tools.battery import analyze_stocks, read_snapshot
from tools.common import clear_cache, data_dir
from tools.discover import compare_baskets, discover_stocks, expand_theme
from tools.filings import get_rpo, get_sec_filings
from tools.fmp import have_key
from tools.fundamentals import (
    get_financials,
    get_historical_valuation,
    get_peers,
    get_quality_scores,
    reverse_dcf,
)
from tools.indicators import get_indicators
from tools.market import (
    get_dividend_info,
    get_earnings_calendar,
    get_etf_info,
    get_market_data,
    get_recent_news,
)
from tools.ownership import get_ownership
from tools.alpaca import (
    paper_account,
    paper_orders,
    paper_place_order,
    paper_positions,
)
from tools.autopilot import autopilot_run, review_autopilot
from tools.risk import risk_metrics
from tools.screen import scan_universe, screen_stocks
from tools.storage import (
    get_evaluation_history,
    query_decisions,
    read_evaluation,
    save_decision,
    save_evaluation,
)

mcp = FastMCP("stock-eval")

# Register tools. Docstrings on each function are the tool contract the model
# reads to decide when to call them — keep them accurate.
# v4: the batch deep battery — the primary tool for multi-name evaluation.
mcp.tool()(analyze_stocks)
mcp.tool()(read_snapshot)  # point-in-time recall of archived scorecards
# v5: discovery — signal-first screens, theme expansion, value-chain lag
mcp.tool()(discover_stocks)
mcp.tool()(expand_theme)
mcp.tool()(compare_baskets)
mcp.tool()(get_market_data)
mcp.tool()(get_etf_info)
mcp.tool()(get_ownership)
mcp.tool()(get_recent_news)
mcp.tool()(get_indicators)
# get_valuation_context was retired in v4: it reported a third, unlabeled P/E
# basis that contradicted the canonical bases (see tools/core.py).
mcp.tool()(get_sec_filings)
mcp.tool()(get_rpo)  # contracted backlog from SEC XBRL — FMP can't supply it
# v2: fundamentals / quality / valuation / peers / reverse-DCF (FMP-backed)
mcp.tool()(get_financials)
mcp.tool()(get_quality_scores)
mcp.tool()(get_historical_valuation)
mcp.tool()(get_peers)
mcp.tool()(reverse_dcf)
# v2: price-based risk (yfinance)
mcp.tool()(risk_metrics)
# v3: screener + earnings calendar + dividend safety
mcp.tool()(screen_stocks)
mcp.tool()(scan_universe)
mcp.tool()(get_earnings_calendar)
mcp.tool()(get_dividend_info)
# autopilot: Alpaca PAPER trading (simulated — no real money)
mcp.tool()(paper_account)
mcp.tool()(paper_positions)
mcp.tool()(paper_place_order)
mcp.tool()(paper_orders)
mcp.tool()(autopilot_run)
mcp.tool()(review_autopilot)
mcp.tool()(save_evaluation)
mcp.tool()(get_evaluation_history)
mcp.tool()(read_evaluation)
mcp.tool()(save_decision)
mcp.tool()(query_decisions)


@mcp.tool()
def healthcheck() -> dict:
    """Verify the data pipeline end-to-end against a known ticker (AAPL).

    Fetches market data, indicators, and SEC filings for Apple and reports
    whether each path is alive. Use this to diagnose the server when
    evaluations come back unexpectedly thin — yfinance can silently break weeks
    after setup, and this tells you in one call whether the data sources are
    reachable rather than leaving you guessing.
    """
    clear_cache(include_disk=True)  # exercise live fetches, not stale cache
    report: dict = {"checked_at": datetime.now(timezone.utc).isoformat(),
                    "data_dir": str(data_dir()), "checks": {}}

    md = get_market_data("AAPL")
    report["checks"]["market_data"] = {
        "ok": "error" not in md and md.get("price") is not None,
        "detail": md.get("error") or f"price={md.get('price')}",
    }

    ind = get_indicators("AAPL", period="1y")
    report["checks"]["indicators"] = {
        "ok": "error" not in ind and ind.get("rsi_14") is not None,
        "detail": ind.get("error") or f"rsi_14={ind.get('rsi_14')}",
    }

    fil = get_sec_filings("AAPL", filing_type="10-K", limit=1)
    report["checks"]["sec_filings"] = {
        "ok": "error" not in fil and fil.get("count", 0) > 0,
        "detail": fil.get("error") or f"found {fil.get('count')} filing(s)",
    }

    if have_key():
        fin = get_financials("AAPL", years=3)
        report["checks"]["fmp_fundamentals"] = {
            "ok": "error" not in fin and fin.get("years", 0) > 0,
            "detail": fin.get("error") or f"{fin.get('years')} years of financials",
        }
    else:
        report["checks"]["fmp_fundamentals"] = {
            "ok": False,
            "detail": "FMP_API_KEY not set — v2 fundamentals tools disabled",
        }

    report["healthy"] = all(c["ok"] for c in report["checks"].values())
    return report


if __name__ == "__main__":
    mcp.run()
