# Stock Evaluation MCP Server

A local [Model Context Protocol](https://modelcontextprotocol.io) server that
gives Claude (Desktop / Code) a set of tools for analyzing individual stocks.
It returns **facts and deterministic calculations only — never
recommendations.** All judgment (the bull/bear/judge synthesis) happens in the
conversation; the server's job is to eliminate hallucinated numbers by sourcing
every figure from data or math.

See [HANDOFF.md](HANDOFF.md) for the full design rationale.

## Tools

| Tool | What it returns |
|------|-----------------|
| `get_market_data(ticker)` | Price, market cap, P/E·P/S·P/B, dividend yield, beta, 52-wk range, margins, growth, cash/debt/FCF, analyst targets, sector, summary. Price cross-checked across two endpoints. |
| `get_indicators(ticker, period="2y")` | RSI(14), SMA 20/50/200, % above SMA50/200, Bollinger position, realized vol (30d & 1y), 1m/3m/6m/1y performance, volume vs 50d avg, plain-language flags. |
| `get_valuation_context(ticker)` | Current forward/trailing P/E and P/S. **Current multiples only** — no historical-range or peer comparison in v1 (see note below). |
| `get_sec_filings(ticker, filing_type="10-Q", limit=4)` | Recent EDGAR filings: date, accession, primary doc, direct sec.gov URL. Metadata only. |
| `get_recent_news(ticker, limit=5)` | Recent headlines: title, publisher, timestamp, link. Fails soft. |
| **`get_financials(ticker, years=3)`** | Multi-year revenue, margins, EPS, FCF, **share count** (dilution vs buybacks), trend flags, and a **`normalized`** block (median margins / avg EPS + one-time-item `outlier_years`) so a single distorted year can't mislead. *(FMP)* |
| **`get_quality_scores(ticker)`** | Piotroski F-score, Altman Z, ROE, ROIC, leverage, interest coverage — plus a sector-agnostic **`distress_composite`** (negative equity / FCF / declining revenue / rising leverage / operating losses) that overrides Altman Z where it's unreliable (retail, financials). *(FMP)* |
| **`get_historical_valuation(ticker, years=3)`** | Is today's P/E·P/S·P/B high or low *for this stock* — min/avg/max range + position. Fills the v1 gap. *(FMP)* |
| **`get_peers(ticker, limit=5)`** | Peer comparison table with each peer's P/E·P/S + market cap, plus peer-group medians. *(FMP)* |
| **`reverse_dcf(ticker)`** | The FCF growth rate the current price is already pricing in — judge plausibility instead of guessing fair value. *(FMP)* |
| **`risk_metrics(ticker, period="3y")`** | Annualized return, volatility, max drawdown, reward-for-risk ratio, downside volatility, best/worst day. |
| **`screen_stocks(tickers, rank_by="value")`** | Poll a basket (list or comma-string) and return a ranked table — forward P/E, FCF yield, margins, growth, dividend, % off 52-wk high, analyst upside, score + verdict (CHEAP-QUALITY / FAIR / EXPENSIVE / SPECULATIVE / DISTRESS). rank_by: value / quality / income / growth. Universal coverage (yfinance). |
| **`get_earnings_calendar(ticker)`** | Next earnings date, days until, recent EPS surprise history. Flags if earnings are ≤14 days out (event risk). |
| **`get_dividend_info(ticker)`** | Yield, payout ratio, FCF coverage, years of history, and per-payment cut detection (last 25y) — is the dividend safe or a warning sign. |
| **`scan_universe(rank_by, top_n, max_weight)`** | Scan the full S&P 500, drop no-anchor names, rank by value/quality/income/growth, return the top N with **confidence weights** (score-proportional, capped). ~4 min. |
| **`paper_account / paper_positions / paper_place_order / paper_orders`** | Alpaca **PAPER** trading (simulated, no real money — endpoint hardcoded to paper). Account, holdings, place order, order history. |
| **`autopilot_run(dry_run=True, top_n)`** | Value/quality autopilot sleeve: scan → confidence-weighted top-N target → rebalance the paper account → **log every trade with its full rationale** to `autopilot_log.jsonl` (the learning dataset). Defaults to dry-run preview. |

**Autopilot:** a paper-only learning experiment, not a profit engine. The point is the logged decision dataset — see HANDOFF.md. Requires `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (paper keys).
| `save_evaluation(ticker, content)` | Writes a markdown evaluation to `data/reports/{TICKER}_{date}.md`. |
| `get_evaluation_history(ticker)` | Lists prior saved evaluations, newest first. |
| `read_evaluation(ticker, date)` | Returns a specific saved evaluation's content. |
| `save_decision(ticker, decision, rationale)` | Appends to `data/decisions.jsonl` (buy/sell/hold/pass/wait) with a +6-month review date. |
| `query_decisions(ticker=None)` | Returns decision-journal entries, optionally filtered. |
| `healthcheck()` | Fetches AAPL across all data paths (incl. FMP) and reports which are alive. Run this first if evaluations look thin. |

Tools marked *(FMP)* require `FMP_API_KEY` (see setup); without it they return a clear "key not set" error and the rest of the server still works.

## Design notes / known limits

- **Loud failures, not silent nulls.** If a data provider is rate-limited or a
  ticker is invalid, data tools return `{"error": ..., "partial": {...}}` rather
  than a deceptively-complete dict of zeros. A missing number is `null` with a
  note, never a fabricated value.
- **yfinance is the data source for market/indicator/valuation tools.** It
  scrapes Yahoo Finance and has no SLA — it can be rate-limited or change schema.
  That's why the healthcheck and the loud-failure contract exist. If results go
  thin weeks from now, call `healthcheck()` to localize the break.
- **Two data sources, by design.** yfinance (no key) backs the live
  market/indicator/risk tools; **Financial Modeling Prep** (key required) backs
  the v2 fundamentals/quality/valuation/peers/reverse-DCF tools, which need
  clean multi-year financials yfinance can't supply reliably. The basic
  `get_valuation_context` stays current-only; **`get_historical_valuation` and
  `get_peers` now provide the historical-range and peer comparison** that were
  deferred in v1.
- **Reverse-DCF, not forward DCF.** `reverse_dcf` reports the growth rate the
  price implies rather than fabricating a fair value — it surfaces an assumption
  to judge, not a false-precision target.
- **Still out of scope:** options analytics / implied volatility, factor-model
  exposure, and full SEC filing-text parsing. These need either pricier data or
  more machinery; revisit deliberately if you actually reach for them.
- 60-second in-memory cache per ticker, so a multi-tool evaluation hits each
  source roughly once.

## Setup

```powershell
cd C:\Users\anura\anu_projects\market_mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Required: SEC User-Agent

SEC EDGAR **blocks requests without a descriptive User-Agent** (you'll get HTTP
403). Set this env var to your name/contact before using `get_sec_filings`:

```
SEC_EDGAR_USER_AGENT="Your Name your@email.com"
```

### Required for the *(FMP)* tools: Financial Modeling Prep API key

The v2 fundamentals/quality/valuation/peers/reverse-DCF tools use Financial
Modeling Prep. Get a key at [financialmodelingprep.com](https://financialmodelingprep.com)
(the **free tier covers single-stock evaluation on a 3-year window**) and set:

```
FMP_API_KEY="your_fmp_key"
```

Note: this key works against FMP's `/stable` API. Without the key, those six
tools return a clear "key not set" error and every other tool still works.

**Free-tier coverage limit (important):** FMP's free tier only serves a subset
of symbols for several endpoints (e.g. `financial-scores`). Large caps like
AAPL/MSFT/INTC work, but many smaller, newer, or delisted names (e.g. IONQ, CEG,
BBBY — and even some large utilities) return HTTP 402 "not available under your
subscription." So the *(FMP)* tools have patchy coverage on the free tier. The
backtest harness in `backtest/` sidesteps this entirely by sourcing fundamentals
from **SEC EDGAR (full US-filer coverage, free)** — promoting that engine to back
the live tools is the planned fix for coverage (see HANDOFF.md).

### Optional: data directory

By default, evaluations and the decision journal are written to `data/` next to
`server.py`. Override with:

```
STOCK_MCP_DATA_DIR=C:\path\to\your\data
```

(Paths are always absolute and anchored to the server file — never the working
directory — because MCP clients launch the server from an unpredictable CWD.)

## Connecting to Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config) and add:

```json
{
  "mcpServers": {
    "stock-eval": {
      "command": "C:\\Users\\anura\\anu_projects\\market_mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\anura\\anu_projects\\market_mcp\\server.py"],
      "env": {
        "SEC_EDGAR_USER_AGENT": "Your Name your@email.com",
        "FMP_API_KEY": "your_fmp_key"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear under the 🔌 menu. Ask it to evaluate a
ticker and it will orchestrate the tools, build bull/bear cases, and defer the
decision to you.

## Connecting to Claude Code

```bash
claude mcp add stock-eval -e SEC_EDGAR_USER_AGENT="Your Name your@email.com" -e FMP_API_KEY="your_fmp_key" -- C:\Users\anura\anu_projects\market_mcp\.venv\Scripts\python.exe C:\Users\anura\anu_projects\market_mcp\server.py
```

## Smoke test

```powershell
$env:SEC_EDGAR_USER_AGENT="Your Name your@email.com"
$env:FMP_API_KEY="your_fmp_key"
.\.venv\Scripts\python.exe -c "import json, server; print(json.dumps(server.healthcheck(), indent=2))"
```

All four checks (`market_data`, `indicators`, `sec_filings`, `fmp_fundamentals`)
should report `"ok": true`.
