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
| **`analyze_stocks(tickers)`** | **The batch deep battery (≤20 names, one call):** per name — 3-lens valuation on canonical live-price bases, quality + sector-aware distress composite, multi-year trends + lifecycle stage + quarterly revenue for sub-scale names (lumpiness flag), contracted backlog (RPO via SEC XBRL), risk vs SPY, momentum, dividend safety, earnings proximity, ownership/short interest, top company-specific headlines, and computed gotcha flags. Includes **basket-relative medians** (the basket is the peer group) and, for non-mature names, a **speculative scorecard**: potential score 0–100 (funding / materiality-gated traction / dilution efficiency / R&D intensity / smart money / asymmetry), TRACTION-vs-STORY bucket, reverse-breakeven years, and the judgment inputs the score can't see. Scores are archived with snapshots so decision reviews can test their predictiveness. Cyclical-resource names (miners, oil) get an honest CYCLICAL-RESOURCE bucket instead of engineering-traction language. ETFs get a fund-shaped scorecard. Runs concurrently server-side (~10s cold, <1s warm for 5 names). Baskets >10 names auto-return in **compact** form (full rows always archived); `compact=true` forces it. Every scorecard is archived to `data/snapshots/` (point-in-time record, one per ticker per day). |
| **`get_rpo(ticker)`** | Remaining performance obligations (contracted backlog) from SEC EDGAR XBRL — latest value, YoY growth, recent series. The decisive commercial metric for early-stage/contract-driven names; a footnote disclosure FMP structurally can't supply. Fails soft for non-taggers. |
| **`discover_stocks(signal)`** | Signal-first, full-market discovery — no sector needed: `fallen_growth` (40%+ off high, revenue still growing 20%+), `quiet_compounders` (FCF yield + growth + thin coverage), `backlog_inflection` (RPO +50% YoY and material vs cap). Returns candidates with the reason each fired. ~1-3 min cold. |
| **`expand_theme(theme)`** | Theme → ranked candidates via ETF holdings: matches theme-named funds (from the full 6,100-ETF list, leveraged wrappers excluded), unions their top holdings, ranks by fund count + combined weight. Automates deep-dive Phase 0. |
| **`compare_baskets(groups)`** | Value-chain lag screen: given {node: tickers} (e.g. AI → chips → power → copper), median 1m/3m/6m/1y perf, forward P/E, % off high per node — shows which chain links the market has repriced and which lag. |
| **`read_snapshot(ticker, date=None)`** | Point-in-time recall: the archived scorecard as of a date (or latest), plus available snapshot dates. For decision reviews — separates "data was wrong" from "judgment was wrong". |
| `get_market_data(ticker)` | Price, market cap, P/E·P/S·P/B (basis-labeled), dividend yield, beta, 52-wk range, margins, growth, cash/debt/FCF, analyst targets, sector, summary. Price cross-checked across two endpoints. Funds are auto-routed to `get_etf_info` instead of erroring. |
| **`get_etf_info(ticker)`** | ETF/fund profile: AUM, expense ratio, distribution yield, category, top-10 holdings with weights, sector weightings, trailing performance. |
| **`get_ownership(ticker)`** | Short interest (% float, days-to-cover, MoM change), institutional/insider ownership %, and 6-month open-market insider buy/sell summary — **officers/directors only** (10%-owner fund distributions reported separately, since they aren't management conviction) *(insider trades need FMP)*. |
| `get_indicators(ticker, period="2y")` | RSI(14), SMA 20/50/200, % above SMA50/200, Bollinger position, realized vol (30d & 1y), 1m/3m/6m/1y performance, volume vs 50d avg, plain-language flags. |
| `get_sec_filings(ticker, filing_type="10-Q", limit=4)` | Recent EDGAR filings: date, accession, primary doc, direct sec.gov URL. Metadata only. |
| `get_recent_news(ticker, limit=5)` | Headlines deduped and filtered for signal: listicle churn dropped by default (`include_generic=True` to see it), each item tagged company-specific or not. Fails soft. |
| **`get_financials(ticker, years=3)`** | Multi-year revenue, margins, EPS, FCF, **share count** (dilution vs buybacks), trend flags, and a **`normalized`** block (median margins / avg EPS + one-time-item `outlier_years`) so a single distorted year can't mislead. *(FMP)* |
| **`get_quality_scores(ticker)`** | Piotroski F-score, Altman Z, ROE, ROIC, leverage, interest coverage — plus a sector-agnostic **`distress_composite`** (negative equity / FCF / declining revenue / rising leverage / operating losses) that overrides Altman Z where it's unreliable (retail, financials). *(FMP)* |
| **`get_historical_valuation(ticker, years=5)`** | Is today's P/E·P/S·P/B high or low *for this stock* — min/avg/max FY-end range, with the **current point computed from the LIVE price** (it no longer lags intraday moves) and every P/E labeled with its EPS basis (ttm vs normalized-median). *(FMP)* |
| **`get_peers(ticker, limit=5)`** | Peer comparison table with each peer's P/E·P/S + market cap, plus peer-group medians. *(FMP)* |
| **`reverse_dcf(ticker)`** | The FCF growth rate the current price is already pricing in. **Distortion-guarded:** when the latest FY deviates sharply from the prior-years median it anchors on the normalized base, reports implied growth on every base, and flags volatile FCF series. *(FMP)* |
| **`risk_metrics(ticker, period="3y")`** | Annualized return, volatility, max drawdown, reward-for-risk ratio, downside volatility, best/worst day, plus **vs-SPY**: benchmark return, excess return, realized beta, correlation. |
| **`screen_stocks(tickers, rank_by="value")`** | Poll a basket (list or comma-string) and return a ranked table — forward P/E, FCF yield, margins, growth, dividend, % off 52-wk high, analyst upside, score + verdict (CHEAP-QUALITY / FAIR / EXPENSIVE / SPECULATIVE / DISTRESS). rank_by: value / quality / income / growth. Universal coverage (yfinance). |
| **`get_earnings_calendar(ticker)`** | Next earnings date, days until, recent EPS surprise history. Flags if earnings are ≤14 days out (event risk). |
| **`get_dividend_info(ticker)`** | Yield, payout ratio, FCF coverage, years of history, and per-payment cut detection (last 25y) — is the dividend safe or a warning sign. |
| **`scan_universe(rank_by, top_n, max_weight)`** | Scan the full S&P 500, drop no-anchor names, rank by value/quality/income/growth, return the top N with **confidence weights** (score-proportional, capped). Parallel — ~1 min cold, seconds warm. |
| **`paper_account / paper_positions / paper_place_order / paper_orders`** | Alpaca **PAPER** trading (simulated, no real money — endpoint hardcoded to paper). Account, holdings, place order, order history. |
| **`autopilot_run(dry_run=True, top_n)`** | Value/quality autopilot sleeve: scan → confidence-weighted top-N target → rebalance the paper account → **log every trade with its full rationale** to `autopilot_log.jsonl` (the learning dataset). Defaults to dry-run preview. |

**Autopilot:** a paper-only learning experiment, not a profit engine. The point is the logged decision dataset — see HANDOFF.md. Requires `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (paper keys).
| `save_evaluation(ticker, content)` | Writes a markdown evaluation to `data/reports/{TICKER}_{date}.md`. |
| `get_evaluation_history(ticker)` | Lists prior saved evaluations, newest first. |
| `read_evaluation(ticker, date)` | Returns a specific saved evaluation's content. |
| `save_decision(ticker, decision, rationale)` | Appends to `data/decisions.jsonl` (buy/sell/hold/pass/wait) with a +6-month review date; auto-links the latest archived scorecard (`snapshot_as_of`) so reviews can recall exactly what was known at decision time. |
| `query_decisions(ticker=None)` | Returns decision-journal entries, optionally filtered. |
| `healthcheck()` | Fetches AAPL across all data paths (incl. FMP) and reports which are alive. Run this first if evaluations look thin. |

Tools marked *(FMP)* require `FMP_API_KEY` (see setup); without it they return a clear "key not set" error and the rest of the server still works.

## Design notes / known limits

- **Canonical valuation bases (v4).** Every multiple derives from one shared
  module (`tools/core.py`): live price, and labeled EPS/FCF bases (TTM vs
  fiscal-year vs normalized-median). Two tools can no longer report different
  P/Es for the same stock on the same day without saying which basis each is.
  (`get_valuation_context` was retired for this reason — it was a third,
  unlabeled P/E source.)
- **Batch-first.** `analyze_stocks` exists because the real cost of an agent
  workflow is tool round-trips × output tokens, not server compute. One call
  replaces ~10 per name; output is a compact scorecard with deterministic
  gotcha flags (distorted FCF base, transitory earnings, Z-score sector
  artifacts) computed server-side.
- **Loud failures, not silent nulls.** If a data provider is rate-limited or a
  ticker is invalid, data tools return `{"error": ..., "partial": {...}}` rather
  than a deceptively-complete dict of zeros. A missing number is `null` with a
  note, never a fabricated value.
- **Source failover (v6).** Daily price history, earnings dates, and dividend
  history are FMP-primary (paid, SLA-backed) with yfinance fallback — the
  yfinance history endpoint was the most rate-limited path in the stack and
  used to blank the risk/momentum layer when throttled. yfinance remains
  primary for the quote/info snapshot and exclusive for short interest,
  holder percentages, and fund holdings. Failed/empty fetches are cached for
  only 60s (negative TTL) so a transient outage isn't pinned for the full
  cache window. If results go thin, `healthcheck()` localizes the break.
- **Two data sources, by design.** yfinance (no key) backs the live
  market/indicator/risk tools; **Financial Modeling Prep** (key required) backs
  the fundamentals/quality/valuation/peers/reverse-DCF tools and insider
  transactions, which need clean multi-year data yfinance can't supply
  reliably.
- **Reverse-DCF, not forward DCF.** `reverse_dcf` reports the growth rate the
  price implies rather than fabricating a fair value — it surfaces an assumption
  to judge, not a false-precision target.
- **Still out of scope:** options analytics / implied volatility, factor-model
  exposure, and full SEC filing-text parsing. These need either pricier data or
  more machinery; revisit deliberately if you actually reach for them.
- **Tiered, disk-backed cache.** Quotes ~15 min, price history 1 h, FMP
  statements/ratios/profiles 24 h (they change quarterly). FMP and quote
  payloads persist to `data/cache/` so repeat runs and server restarts don't
  refetch. `healthcheck()` clears both layers to exercise live paths.

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
