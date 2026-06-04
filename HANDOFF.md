# Stock Evaluation MCP Server — Build Specification

## Purpose

Build a Model Context Protocol (MCP) server that gives Claude a set of tools for
analyzing individual stocks. The server runs locally, is consumed by Claude
Desktop / Claude Code (so it uses the user's existing Pro subscription rather than
the paid API), and exists to support human investment decisions — not to trade
autonomously.

The design principle is a clean separation of responsibilities:
- **The MCP tools** fetch authoritative data and compute objective numbers.
- **Claude** orchestrates the tools, interprets results, and synthesizes analysis.
- **The human** makes the final decision.

This means the MCP server should be "dumb and reliable": it returns facts and
calculations, never recommendations. All judgment lives in the conversation layer.

---

## Architecture

```
┌──────────────────────────────────────────────┐
│  Claude (Desktop / Code) — orchestration       │
│  Calls tools, interprets, runs bull/bear/judge │
└──────────────────────────────────────────────┘
                     ↕  (MCP protocol)
┌──────────────────────────────────────────────┐
│  MCP SERVER (this project) — facts & math      │
│  - market data tools                            │
│  - SEC filing tools                             │
│  - indicator/valuation tools                    │
│  - news tools                                   │
│  - persistence tools (save/retrieve reports)    │
└──────────────────────────────────────────────┘
                     ↕
┌──────────────────────────────────────────────┐
│  DATA SOURCES                                   │
│  yfinance · SEC EDGAR · local disk (reports/)   │
└──────────────────────────────────────────────┘
```

The bull/bear/judge analysis pattern is NOT implemented as code. Claude performs
it natively in the conversation, using the objective data the tools return. This
keeps the server simple and lets the reasoning layer evolve without code changes.

---

## Tech stack

- Python 3.11+
- `mcp` (official Model Context Protocol SDK)
- `yfinance` — market data
- `sec-edgar-downloader` or direct EDGAR REST calls — filings
- `pandas`, `numpy` — calculations
- Standard library `json`, `pathlib`, `datetime` for persistence

No API key needed for the model (Pro covers it via the MCP client). SEC EDGAR
requires a descriptive User-Agent string (their policy), configurable via env var.

---

## Tools to implement

### Data tools
1. `get_market_data(ticker)` → dict
   Current price, market cap, forward/trailing P/E, P/S, P/B, dividend yield,
   beta, 52-week range, margins, revenue/earnings growth, cash, debt, FCF,
   analyst target mean/high/low, recommendation, # analysts, sector/industry,
   short business summary.

2. `get_indicators(ticker, period="2y")` → dict
   RSI(14), SMA 20/50/200, % above SMA50/200, Bollinger position,
   realized volatility (30d & 1y annualized), performance over 1m/3m/6m/1y,
   recent volume vs 50-day average. Also return a list of plain-language
   "flags" (e.g. "RSI overbought at 78", "up 110% in past year").

3. `get_valuation_context(ticker)` → dict
   Current forward P/E and P/S compared against the stock's own trailing
   history where available; flag whether current multiples are high/low vs
   the stock's range. (Peer comparison optional in v1.)

4. `get_sec_filings(ticker, filing_type="10-Q", limit=4)` → list
   Resolve ticker → CIK via EDGAR company_tickers.json, then list recent
   filings of the requested type with date, accession number, and URL.
   (Full filing-text extraction can be a v2 enhancement.)

5. `get_recent_news(ticker, limit=5)` → list
   Recent headlines with title, publisher, timestamp, link.

### Persistence tools (where local storage earns its keep)
6. `save_evaluation(ticker, content)` → path
   Write a markdown evaluation to `reports/{TICKER}_{YYYY-MM-DD}.md`.

7. `get_evaluation_history(ticker)` → list
   List prior saved evaluations for a ticker, newest first, with dates.

8. `read_evaluation(ticker, date)` → str
   Return the content of a specific saved evaluation.

9. `save_decision(ticker, decision, rationale)` → path
   Append a structured entry to a decision journal (`decisions.jsonl`):
   timestamp, ticker, decision (buy/sell/hold/pass/wait), rationale,
   and a "review_after" date (default +6 months).

10. `query_decisions(ticker=None)` → list
    Return decision-journal entries, optionally filtered by ticker.

---

## File structure

```
stock-mcp-server/
├── HANDOFF.md            # this file
├── README.md
├── requirements.txt
├── server.py             # MCP server entry point, tool registration
├── tools/
│   ├── __init__.py
│   ├── market.py         # get_market_data, get_recent_news
│   ├── indicators.py     # get_indicators, get_valuation_context
│   ├── filings.py        # get_sec_filings
│   └── storage.py        # save/read evaluations, decision journal
├── reports/              # saved evaluations (gitignored)
└── decisions.jsonl       # decision journal (gitignored)
```

---

## Quality bars

- Every tool wraps external calls in try/except and returns a structured
  `{"error": "..."}` rather than throwing — Claude should always get a usable
  response, never a crashed server.
- All numbers come from data sources or deterministic calculation, never from
  the model. This is the whole point: it eliminates numerical hallucination.
- Outputs are JSON-serializable and human-inspectable.
- Tool docstrings are clear and specific — Claude reads them to decide when to
  call each tool, so they double as the tool's "API contract."
- Missing data is returned as null with a note, not silently dropped.

---

## Explicitly out of scope for v1

- No autonomous trading or order execution.
- No backtesting engine.
- No web dashboard.
- No ML price-prediction models.
- No full SEC filing text parsing (metadata only for now).
- No peer-group comparison (single-stock context only for now).
- No portfolio/tax-lot tooling (separate future project).

These are deliberate cuts to ship a working v1. Revisit only after the core tool
has been used enough to reveal which additions are actually wanted.

---

## Suggested build order

1. Project skeleton + `requirements.txt` + minimal `server.py` that registers
   one trivial tool, confirm it connects to the MCP client.
2. `get_market_data` — verify real data flows through to Claude.
3. `get_indicators` — add the calculation layer.
4. `get_sec_filings` + `get_recent_news`.
5. `get_valuation_context`.
6. Persistence tools (`save_evaluation`, history, decision journal).
7. README with setup + MCP client configuration instructions.

Ship and use after step 6; everything else is enhancement.

---

## How Claude should use the tools (note for the conversation layer, not code)

When asked to evaluate a stock, the intended pattern is:
1. Gather objective data:
   - Snapshot/technical: `get_market_data`, `get_indicators`, `risk_metrics`,
     `get_recent_news`, `get_sec_filings`.
   - Fundamentals/quality: `get_financials` (read the `normalized` block, not
     just the latest year), `get_quality_scores` (read `distress_composite`,
     not just Altman Z), `get_historical_valuation`, `get_peers`.
   - Forward framing: `reverse_dcf` — what growth the price already implies.
2. Construct the strongest BULL case from that objective data.
3. Construct the strongest BEAR case from the same data.
4. Act as JUDGE: weigh both, distinguish facts from interpretation, state what
   is contested vs settled, give a probability-weighted view and the conditions
   that would change it. Defer the final decision to the human.
5. Optionally `save_evaluation` and, if the human decides, `save_decision`.

The server provides the facts; this pattern provides the rigor.

### Lessons baked in from the judgment-quality backtest (CSCO/NVDA/META/BBBY)

These are not optional niceties — each fixed a real way the evaluation went
wrong in testing:

- **Cheap ≠ bargain. Always separate "cheap because hated" from "cheap because
  dying."** A low P/E with intact margins, positive FCF, and a solid balance
  sheet (META @ 8.5x in 2022) is opportunity; a low P/S with negative margins,
  negative FCF, and negative equity (BBBY 2022) is a value trap. The
  `distress_composite` is the decisive tell — check it before calling anything
  "cheap."
- **Do not trust Altman Z alone for retailers, financials, or asset-light
  firms.** It rated BBBY and (currently) INTC "safe"/healthy near acute
  trouble. The `distress_composite` is sector-agnostic and overrides it.
- **Normalize before judging valuation.** A single year with a one-time item
  (CSCO's FY2018 tax charge → P/E ~2900) is meaningless. Use the `normalized`
  median margins / average EPS and heed `outlier_years`.
- **Trailing fundamentals cannot price an explosive growth inflection** (NVDA
  2022–23 looked egregiously expensive on every trailing metric, then ran
  10x+). When `reverse_dcf` shows the price implies extreme growth, frame the
  decision honestly as "this is a bet on the forward story, not a value-
  supported buy" rather than mislabeling it cheap or expensive.
- **Oversold technicals (low RSI) are a trap on fundamentally broken names** —
  BBBY was "oversold" for most of its decline. Let fundamentals veto a
  technical bounce thesis.
