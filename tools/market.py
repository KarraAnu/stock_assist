"""Market data + news tools (yfinance-backed)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import yfinance as yf

from .common import (
    get_dividends_raw,
    get_earnings_dates_raw,
    get_fast_info,
    get_history,
    get_info,
    get_news_raw,
    missing_data_error,
    normalize_ticker,
    num,
)


def get_market_data(ticker: str) -> dict:
    """Fetch core fundamentals and market snapshot for a single stock.

    Returns current price, market cap, valuation multiples (forward/trailing
    P/E, P/S, P/B), dividend yield, beta, 52-week range, margins,
    revenue/earnings growth, cash/debt/free-cash-flow, analyst target
    mean/high/low and recommendation, sector/industry, and a short business
    summary.

    Numbers come directly from the data provider, never inferred. Price is
    cross-checked between two independent provider endpoints; if they disagree
    materially a "price_crosscheck" warning is included so the discrepancy is
    visible rather than silently trusted.

    If critical fields (price, market cap) are unavailable — which usually means
    the provider was rate-limited or the ticker is invalid/delisted — this
    returns an {"error": ..., "partial": {...}} payload instead of a
    deceptively-complete dict. Treat that as "data unavailable", not as zeros.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    info = get_info(ticker)
    fast = get_fast_info(ticker)

    # ETFs/funds have no market cap or margins — the old code hard-errored on
    # them (NUKZ/NLR/URA in dogfooding). Route to the fund-shaped payload
    # instead of failing.
    if info.get("quoteType") in ("ETF", "MUTUALFUND"):
        etf = get_etf_info(ticker)
        etf["note"] = (f"{ticker} is a fund, not a single stock — returning "
                       f"get_etf_info payload (stock fields like margins/market "
                       f"cap don't apply)")
        return etf

    info_price = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    fast_price = num(fast.get("last_price"))
    price = info_price if info_price is not None else fast_price
    market_cap = num(info.get("marketCap")) or num(fast.get("market_cap"))

    # --- validation gate: fail loudly if the essentials are missing ---
    missing = []
    if price is None:
        missing.append("price")
    if market_cap is None:
        missing.append("market_cap")
    if missing:
        return missing_data_error(ticker, missing, partial={
            "price": price,
            "market_cap": market_cap,
            "name": info.get("shortName") or info.get("longName"),
        })

    warnings = []

    # --- price cross-check between the two endpoints ---
    if info_price is not None and fast_price is not None and info_price > 0:
        diff_pct = abs(info_price - fast_price) / info_price * 100
        if diff_pct > 2.0:
            warnings.append(
                f"price_crosscheck: .info=${info_price:.2f} vs "
                f".fast_info=${fast_price:.2f} differ by {diff_pct:.1f}% — "
                f"data may be stale on one endpoint"
            )

    summary = info.get("longBusinessSummary")
    if summary and len(summary) > 600:
        summary = summary[:600].rstrip() + "…"

    result = {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "currency": info.get("currency") or fast.get("currency"),
        "price": price,
        "market_cap": market_cap,
        "valuation": {
            "forward_pe": num(info.get("forwardPE")),
            "trailing_pe": num(info.get("trailingPE")),
            "price_to_sales_ttm": num(info.get("priceToSalesTrailing12Months")),
            "price_to_book": num(info.get("priceToBook")),
            "basis": "live price; trailing_pe = TTM EPS incl. one-time items "
                     "(for the normalized basis see get_historical_valuation "
                     "or analyze_stocks)",
        },
        "dividend_yield": num(info.get("dividendYield")),
        "beta": num(info.get("beta")),
        "fifty_two_week": {
            "high": num(info.get("fiftyTwoWeekHigh")) or num(fast.get("year_high")),
            "low": num(info.get("fiftyTwoWeekLow")) or num(fast.get("year_low")),
        },
        "margins": {
            "gross": num(info.get("grossMargins")),
            "operating": num(info.get("operatingMargins")),
            "profit": num(info.get("profitMargins")),
        },
        "growth": {
            "revenue": num(info.get("revenueGrowth")),
            "earnings": num(info.get("earningsGrowth"))
            if info.get("earningsGrowth") is not None
            else num(info.get("earningsQuarterlyGrowth")),
        },
        "balance_sheet": {
            "total_cash": num(info.get("totalCash")),
            "total_debt": num(info.get("totalDebt")),
            "free_cash_flow": num(info.get("freeCashflow")),
        },
        "analyst": {
            "target_mean": num(info.get("targetMeanPrice")),
            "target_high": num(info.get("targetHighPrice")),
            "target_low": num(info.get("targetLowPrice")),
            "recommendation": info.get("recommendationKey"),
            "recommendation_mean": num(info.get("recommendationMean")),
            "num_analysts": num(info.get("numberOfAnalystOpinions")),
        },
        "business_summary": summary,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def get_etf_info(ticker: str) -> dict:
    """ETF / fund profile: AUM, expense ratio, yield, top holdings, sector mix.

    For thematic investing the fund is often the natural instrument (NLR, URA,
    NUKZ...), and stock-shaped tools error on funds (no market cap, no
    margins). This returns the fund-shaped facts instead: assets under
    management, expense ratio, distribution yield, category, top-10 holdings
    with weights, sector weightings, and trailing performance. Holdings data
    comes from the provider and may lag a few weeks. Fails soft on fields the
    provider doesn't supply.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    info = get_info(ticker)
    fast = get_fast_info(ticker)
    price = (num(info.get("regularMarketPrice")) or num(info.get("navPrice"))
             or num(fast.get("last_price")))
    if price is None:
        return missing_data_error(ticker, ["price"], partial={
            "name": info.get("shortName") or info.get("longName")})

    def produce_funds_data() -> dict:
        holdings, sectors, overview, expense = [], {}, {}, None
        try:
            fd = yf.Ticker(ticker).funds_data
            try:
                th = fd.top_holdings
                if th is not None and not th.empty:
                    for sym, row in th.iterrows():
                        w = num(row.get("Holding Percent"))
                        holdings.append({
                            "symbol": str(sym),
                            "name": row.get("Name"),
                            "weight_pct": round(w * 100, 2) if w is not None else None,
                        })
            except Exception:
                pass
            try:
                sw = fd.sector_weightings
                if isinstance(sw, dict):
                    sectors = {k: round(float(v) * 100, 2)
                               for k, v in sw.items() if num(v)}
            except Exception:
                pass
            try:
                ov = fd.fund_overview
                if isinstance(ov, dict):
                    overview = ov
            except Exception:
                pass
            try:
                ops = fd.fund_operations
                if ops is not None and not ops.empty:
                    er = ops.loc["Annual Report Expense Ratio"].iloc[0]
                    expense = round(float(er) * 100, 2) if er == er else None
            except Exception:
                pass
        except Exception:
            pass  # funds_data unavailable — info-level fields still useful
        return {"holdings": holdings, "sectors": sectors,
                "overview": overview, "expense_ratio": expense}

    from .common import TTL_HISTORY, cached
    funds = cached(("funds_data", ticker), produce_funds_data,
                   ttl=TTL_HISTORY, disk=True)
    holdings, sectors, overview = funds["holdings"], funds["sectors"], funds["overview"]
    expense_ratio = num(info.get("netExpenseRatio"))
    if expense_ratio is None:
        expense_ratio = funds["expense_ratio"]

    # Trailing performance from price history (2y so the 1y point exists).
    perf = {}
    hist = get_history(ticker, period="2y")
    if hist is not None and not hist.empty and "Close" in hist:
        close = hist["Close"].dropna()
        for label, days in (("1m", 21), ("3m", 63), ("6m", 126), ("1y", 252)):
            if len(close) > days and close.iloc[-(days + 1)] > 0:
                perf[label] = round(
                    float(close.iloc[-1] / close.iloc[-(days + 1)] - 1) * 100, 2)

    y = num(info.get("yield")) or num(info.get("dividendYield"))
    return {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName"),
        "instrument_type": info.get("quoteType") or "ETF",
        "category": info.get("category") or overview.get("categoryName"),
        "fund_family": info.get("fundFamily") or overview.get("family"),
        "price": price,
        "aum": num(info.get("totalAssets")),
        "expense_ratio_pct": expense_ratio,
        "distribution_yield_pct": round(y * 100, 2) if (y is not None and y < 1) else y,
        "beta_3y": num(info.get("beta3Year")),
        "ytd_return_pct": (round(num(info.get("ytdReturn")) * 100, 2)
                           if num(info.get("ytdReturn")) is not None else None),
        "three_year_avg_return_pct": (round(num(info.get("threeYearAverageReturn")) * 100, 2)
                                      if num(info.get("threeYearAverageReturn")) is not None else None),
        "performance_pct": perf,
        "top_holdings": holdings,
        "sector_weights_pct": sectors,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "note": "holdings/sector data is provider-supplied and may lag; for "
                "single-stock analysis of a holding, use the stock tools",
    }


# Titles that are listicle/clickbait churn rather than company news. These
# dominated dogfooding output ("5 stocks winning the AI race" echoed across
# every ticker) and drowned the real catalysts.
_GENERIC_TITLE_RE = re.compile(
    r"(^\d+\s|\b\d+\s+(best|top|magnificent|cheap|great|hot|super)?\s*"
    r"(stocks?|etfs?|picks|reasons|things|ways|moves|buys)\b"
    r"|\bbetter buy\b|^prediction[:\s]|^should you buy\b|^is it too late\b"
    r"|\bmillionaire\b|\bbuy the dip\b|^if you.?d invested\b)",
    re.IGNORECASE,
)


def _company_tokens(ticker: str, info: dict) -> list[str]:
    """Tokens that mark a headline as company-specific: the ticker plus the
    distinctive first word(s) of the company name."""
    tokens = [ticker.lower()]
    name = (info.get("shortName") or info.get("longName") or "")
    for word in re.split(r"[\s,.]+", name)[:2]:
        w = word.lower().strip("&()")
        if len(w) >= 4 and w not in ("corp", "inc", "company", "holdings",
                                     "group", "energy", "the"):
            tokens.append(w)
    return tokens


def _news_timestamp(item: dict) -> str | None:
    """Defensively extract an ISO timestamp across yfinance schema variants."""
    content = item.get("content") if isinstance(item.get("content"), dict) else None
    # Newer schema: content.pubDate is an ISO string.
    if content:
        for key in ("pubDate", "displayTime"):
            val = content.get(key)
            if val:
                return str(val)
    # Older schema: providerPublishTime is a unix epoch int.
    epoch = item.get("providerPublishTime")
    if epoch:
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
        except (ValueError, OSError, TypeError):
            return None
    return None


def get_recent_news(ticker: str, limit: int = 5,
                    include_generic: bool = False) -> dict:
    """Recent news headlines for a ticker, filtered for signal.

    Deduplicates repeated stories and tags each headline as company-specific
    or generic. Listicle/clickbait churn ("5 stocks winning the AI race") is
    filtered out by default — pass include_generic=True to see everything.
    Returns title, publisher, timestamp, link per item, plus how many items
    were filtered.

    News is the least critical input and its provider schema is unstable, so
    this fails soft: if the feed is unavailable or unparseable it returns
    {"error": "news unavailable", "items": []} rather than blocking an
    evaluation.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided", "items": []}

    raw = get_news_raw(ticker)
    if not raw:
        return {"error": "news unavailable", "ticker": ticker, "items": []}

    tokens = _company_tokens(ticker, get_info(ticker))
    parsed, seen_titles = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else None

        # title
        title = item.get("title") or (content.get("title") if content else None)

        # publisher
        publisher = item.get("publisher")
        if not publisher and content:
            prov = content.get("provider")
            if isinstance(prov, dict):
                publisher = prov.get("displayName")

        # link
        link = item.get("link")
        if not link and content:
            cu = content.get("clickThroughUrl") or content.get("canonicalUrl")
            if isinstance(cu, dict):
                link = cu.get("url")

        if not title:
            continue
        # Dedup: the same wire story echoes across tickers/publishers.
        key = re.sub(r"[^a-z0-9]+", "", title.lower())
        if key in seen_titles:
            continue
        seen_titles.add(key)

        title_l = title.lower()
        parsed.append({
            "title": title,
            "publisher": publisher,
            "published": _news_timestamp(item),
            "link": link,
            "company_specific": any(t in title_l for t in tokens),
            "generic": bool(_GENERIC_TITLE_RE.search(title)),
        })

    if not parsed:
        return {"error": "news unavailable (unrecognized feed schema)",
                "ticker": ticker, "items": []}

    # Company-specific, non-listicle headlines first.
    parsed.sort(key=lambda i: (not i["company_specific"], i["generic"]))
    kept = parsed if include_generic else [i for i in parsed if not i["generic"]]
    if not kept:  # everything was listicle churn — better to show it than nothing
        kept = parsed
    n = max(0, int(limit))
    return {"ticker": ticker, "items": kept[:n],
            "filtered_out": len(parsed) - min(len(kept), n)}


def get_earnings_calendar(ticker: str) -> dict:
    """Next earnings date and recent earnings-surprise history for a stock.

    Returns the next scheduled earnings date and days until it, plus the last
    few quarters of EPS estimate vs. actual and the surprise %. Use this before
    forming a view — evaluating a stock days before it reports carries event
    risk the fundamentals don't show. Fails soft if the schedule is unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    tk = yf.Ticker(ticker)
    edf = get_earnings_dates_raw(ticker)

    next_date, days_until, surprises = None, None, []
    today = date.today()
    if edf is not None and not edf.empty:
        for idx, row in edf.iterrows():
            try:
                d = idx.date()
            except Exception:
                continue
            reported = row.get("Reported EPS")
            est = row.get("EPS Estimate")
            if reported is None or (isinstance(reported, float) and reported != reported):
                # future / not yet reported
                if d >= today and (next_date is None or d < next_date):
                    next_date = d
            else:
                surprises.append({
                    "date": d.isoformat(),
                    "eps_estimate": num(est),
                    "eps_actual": num(reported),
                    "surprise_pct": num(row.get("Surprise(%)")),
                })
        surprises = sorted(surprises, key=lambda s: s["date"], reverse=True)[:4]

    if next_date is None:
        # fall back to the calendar object
        try:
            cal = tk.calendar
            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if ed:
                cand = ed[0] if isinstance(ed, (list, tuple)) else ed
                next_date = cand if isinstance(cand, date) else None
        except Exception:
            pass

    # A stale schedule can leave only past dates — a date in the past is not a
    # "next" earnings date (dogfooding: RGTI returned days_until=-29).
    stale_note = None
    if next_date is not None and next_date < today:
        stale_note = (f"provider schedule is stale (last known date "
                      f"{next_date.isoformat()} already passed); next date not "
                      f"yet published")
        next_date = None

    if next_date:
        days_until = (next_date - today).days

    if next_date is None and not surprises:
        return {"error": "earnings schedule unavailable", "ticker": ticker}

    result = {
        "ticker": ticker,
        "next_earnings_date": next_date.isoformat() if next_date else None,
        "days_until": days_until,
        "recent_surprises": surprises,
    }
    if stale_note:
        result["note"] = stale_note
    if days_until is not None and 0 <= days_until <= 14:
        result["flag"] = (f"earnings in {days_until} days — material event risk; "
                          f"a view formed now could be invalidated on the report")
    return result


def get_dividend_info(ticker: str) -> dict:
    """Dividend yield, payout sustainability, and cut history for a stock.

    Returns current yield, payout ratio, free-cash-flow coverage of the
    dividend, and a scan of annual dividend history for past cuts. Use it to
    judge whether a high yield is safe or a warning sign. For non-payers it
    says so. Fails soft if dividend data is unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    info = get_info(ticker)
    div_yield = num(info.get("dividendYield"))
    rate = num(info.get("trailingAnnualDividendRate"))
    payout_ratio = num(info.get("payoutRatio"))
    shares = num(info.get("sharesOutstanding"))
    fcf = num(info.get("freeCashflow"))

    if not div_yield and not rate:
        return {"ticker": ticker, "pays_dividend": False,
                "note": "no dividend currently paid"}

    # FCF coverage: free cash flow / total dividends paid.
    fcf_coverage = None
    if fcf and rate and shares:
        total_div = rate * shares
        if total_div > 0:
            fcf_coverage = round(fcf / total_div, 2)

    # Cut scan: build annual dividend totals from the payment history.
    cuts, years_paid = [], None
    try:
        divs = get_dividends_raw(ticker)
        if divs is not None and not divs.empty:
            payments = [(ts, float(amt)) for ts, amt in divs.items() if float(amt) > 0]
            if payments:
                years_paid = len({ts.year for ts, _ in payments})
            # Detect cuts at the PER-PAYMENT level: a real cut is when the
            # per-share payment steps down vs the prior one. This is immune to
            # how many ex-dates land in a calendar year (the annual-sum method's
            # flaw). 20% threshold filters minor variable-payment noise. Limit
            # to the last ~25 years: that's the decision-relevant window, and it
            # drops pre-1990 stock-split artifacts in the raw data.
            cutoff_year = date.today().year - 25
            recent = [(ts, amt) for ts, amt in payments if ts.year >= cutoff_year]
            for (pts, pa), (cts, ca) in zip(recent, recent[1:]):
                if pa > 0 and ca < pa * 0.80:
                    cuts.append({"date": cts.date().isoformat(),
                                 "from_per_share": round(pa, 4),
                                 "to_per_share": round(ca, 4)})
    except Exception:
        pass

    flags = []
    if fcf_coverage is not None and fcf_coverage < 1:
        flags.append(f"dividend NOT covered by free cash flow (coverage {fcf_coverage}x) "
                     f"— elevated cut risk")
    elif fcf_coverage is not None and fcf_coverage >= 1.5:
        flags.append(f"dividend well covered by FCF ({fcf_coverage}x)")
    if payout_ratio is not None and payout_ratio > 0.9:
        flags.append(f"payout ratio {payout_ratio*100:.0f}% of earnings — little cushion")
    if cuts:
        flags.append(f"cut its dividend {len(cuts)} time(s) historically "
                     f"(most recent: {cuts[-1]['date']})")

    return {
        "ticker": ticker,
        "pays_dividend": True,
        "dividend_yield_pct": div_yield,
        "annual_rate_per_share": rate,
        "payout_ratio": payout_ratio,
        "fcf_coverage": fcf_coverage,
        "years_of_history": years_paid,
        "past_cuts": cuts,
        "flags": flags,
    }
