"""Fundamentals, quality, historical valuation, peers, reverse-DCF (FMP-backed).

These are the v2 tools. They use Financial Modeling Prep's /stable API for
clean multi-year financials the free market-data source can't supply reliably.
All default to a 3-year window. Every tool fails loudly if FMP is unavailable.
"""

from __future__ import annotations

from .common import get_info, normalize_ticker, num
from .core import fcf_bases, live_market_cap, pe_bases
from .fmp import FMPError, fmp_get, have_key


def _degraded(ticker: str, reason, tool: str) -> dict:
    """Graceful degradation when FMP is unavailable (402 tier/coverage, no key,
    or an outage). Instead of a bare error, fall back to the yfinance current
    snapshot so the tool still returns usable current-period data.
    """
    msg = str(reason)
    not_covered = ("402" in msg
                   or "not available under your current subscription" in msg)
    no_key = msg == "no_key"
    info = get_info(ticker) or {}
    snapshot = {
        "revenue_ttm": num(info.get("totalRevenue")),
        "gross_margin_pct": _pct(info.get("grossMargins")),
        "operating_margin_pct": _pct(info.get("operatingMargins")),
        "net_margin_pct": _pct(info.get("profitMargins")),
        "free_cash_flow": num(info.get("freeCashflow")),
        "total_cash": num(info.get("totalCash")),
        "total_debt": num(info.get("totalDebt")),
        "trailing_pe": num(info.get("trailingPE")),
        "forward_pe": num(info.get("forwardPE")),
        "price_to_sales_ttm": num(info.get("priceToSalesTrailing12Months")),
        "revenue_growth": _pct(info.get("revenueGrowth")),
    } if info else {}
    if no_key:
        why = "FMP_API_KEY not set"
    elif not_covered:
        why = ("FMP does not cover this symbol on the current subscription tier "
               "(upgrade FMP, or this name needs the SEC source)")
    else:
        why = f"FMP error: {msg[:140]}"
    return {
        "ticker": ticker,
        "error": why,
        "degraded": True,
        "fmp_status": ("no_key" if no_key else
                       "not_covered" if not_covered else "unavailable"),
        "fallback_source": "yfinance current snapshot",
        "current_snapshot": snapshot,
        "note": (f"{tool} needs FMP coverage (paid tier) or the SEC engine for "
                 f"multi-year history/scores; returning current-period data only."),
    }


def _fmp_unavailable(exc: Exception) -> dict:
    return {"error": f"FMP unavailable: {exc}"}


def _pct(x) -> float | None:
    v = num(x)
    return round(v * 100, 2) if v is not None else None


def get_financials(ticker: str, years: int = 3) -> dict:
    """Multi-year fundamentals trend for a single stock (default 3 years).

    Returns, per fiscal year (newest first): revenue, gross/operating/net
    margins, diluted EPS, free cash flow and FCF margin, and diluted shares
    outstanding. The share-count series reveals dilution vs buybacks — value
    created or destroyed per share that a single-point snapshot hides.

    Also returns plain-language "trends" (e.g. "revenue grew 3 straight years",
    "net margin compressed from 25% to 19%", "share count down 4% — buybacks").

    Sourced from Financial Modeling Prep. Returns {"error": ...} if FMP is
    unavailable or the ticker has no fundamentals.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not have_key():
        return _degraded(ticker, "no_key", "get_financials")

    n = max(1, min(int(years), 10))
    try:
        income = fmp_get("income-statement", symbol=ticker, period="annual", limit=n)
        cash = fmp_get("cash-flow-statement", symbol=ticker, period="annual", limit=n)
        balance = fmp_get("balance-sheet-statement", symbol=ticker, period="annual", limit=1)
    except FMPError as exc:
        return _degraded(ticker, exc, "get_financials")

    if not income:
        return _degraded(ticker, "402", "get_financials")

    cash_by_year = {str(c.get("fiscalYear")): c for c in cash} if cash else {}

    rows = []
    for stmt in income:
        fy = str(stmt.get("fiscalYear"))
        rev = num(stmt.get("revenue"))
        gross = num(stmt.get("grossProfit"))
        op = num(stmt.get("operatingIncome"))
        ni = num(stmt.get("netIncome"))
        cf = cash_by_year.get(fy, {})
        fcf = num(cf.get("freeCashFlow"))
        rows.append({
            "fiscal_year": fy,
            "revenue": rev,
            "gross_margin_pct": round(gross / rev * 100, 2) if rev and gross else None,
            "operating_margin_pct": round(op / rev * 100, 2) if rev and op else None,
            "net_margin_pct": round(ni / rev * 100, 2) if rev and ni else None,
            "eps_diluted": num(stmt.get("epsDiluted")),
            "free_cash_flow": fcf,
            "fcf_margin_pct": round(fcf / rev * 100, 2) if rev and fcf else None,
            "diluted_shares": num(stmt.get("weightedAverageShsOutDil")),
            "stock_based_comp": num(cf.get("stockBasedCompensation")),
        })

    trends = _financial_trends(rows)
    normalized = _normalized(rows)
    bal = balance[0] if balance else {}
    fy_cash = num(bal.get("cashAndShortTermInvestments")) or num(bal.get("cashAndCashEquivalents"))
    # Most-recent-quarter cash from yfinance: FY-end cash misses mid-year
    # raises in exactly the cash-burning names where runway matters (RGTI's
    # $350M ATM made the FY-based runway read 0.6yr against ~$600M actual).
    mrq_cash = num(get_info(ticker).get("totalCash"))
    stage = _company_stage(rows, fy_cash, mrq_cash)

    result = {"ticker": ticker, "years": len(rows), "annual": rows,
              "trends": trends, "normalized": normalized,
              "company_stage": stage,
              "source": "Financial Modeling Prep"}

    # Billed-backlog proxy. The full contracted backlog (RPO) is a footnote
    # disclosure FMP can't supply — see get_rpo (SEC XBRL).
    d_cur = num(bal.get("deferredRevenue"))
    d_non = num(bal.get("deferredRevenueNonCurrent"))
    if d_cur or d_non:
        result["deferred_revenue"] = {
            "current": d_cur, "non_current": d_non,
            "note": "FY-end billed-backlog proxy; full contracted backlog is "
                    "RPO — see get_rpo",
        }

    quarterly = _quarterly_revenue(ticker, rows)
    if quarterly:
        result["quarterly"] = quarterly
    return result


def _quarterly_revenue(ticker: str, rows: list[dict]) -> dict | None:
    """Last 8 quarters of revenue for sub-scale names (revenue < $500M).

    Annual data hides the lumpiness of system-sale businesses: QBTS showed
    "revenue grew every year" (fiscal years) AND "-81% growth" (TTM) at the
    same time — both true, because one quarter carried a one-time system sale.
    The quarterly series makes that visible deterministically.
    """
    latest_rev = rows[0].get("revenue") if rows else None
    if latest_rev is not None and latest_rev >= 500_000_000:
        return None
    try:
        qinc = fmp_get("income-statement", symbol=ticker, period="quarter", limit=8)
    except FMPError:
        return None
    if not qinc:
        return None
    import statistics
    by_q, vals = {}, []
    for q in qinc:
        label = f"{q.get('fiscalYear')}-{q.get('period')}"
        rv = num(q.get("revenue"))
        by_q[label] = round(rv / 1e6, 2) if rv is not None else None
        if rv is not None:
            vals.append(rv)
    out = {"revenue_by_quarter_usd_m": by_q}
    if len(vals) >= 4:
        med = statistics.median(vals)
        if med > 0 and max(vals) > 2.5 * med:
            out["flag"] = ("revenue is LUMPY — one quarter is >2.5x the median "
                           "(likely one-time system sales); single-period YoY "
                           "comps mislead in both directions")
    return out


def _company_stage(rows: list[dict], fy_cash: float | None,
                   mrq_cash: float | None = None) -> dict:
    """Classify lifecycle stage and (for cash-burners) the cash runway.

    Pre-revenue/early-stage names can't be judged on P/E or margins — the
    decisive question is how long the cash lasts. Runway prefers the
    most-recent-quarter cash figure over fiscal-year-end (which goes stale the
    moment a company raises), and labels which one it used.
    """
    latest = rows[0] if rows else {}
    rev = latest.get("revenue")
    ni_positive_years = sum(1 for r in rows
                            if r.get("net_margin_pct") is not None and r["net_margin_pct"] > 0)
    fcf = latest.get("free_cash_flow")

    if rev is None or rev < 10_000_000:
        stage = "pre-revenue / story stock"
    elif rev < 100_000_000 and ni_positive_years == 0:
        stage = "early-stage / pre-profit"
    elif ni_positive_years >= max(1, len(rows) - 1):
        stage = "mature / consistently profitable"
    else:
        stage = "transitional / inconsistent profitability"

    cash = mrq_cash if mrq_cash is not None else fy_cash
    cash_as_of = "most recent quarter (yfinance)" if mrq_cash is not None \
        else "fiscal-year-end (FMP)"

    runway = None
    if fcf is not None and fcf < 0 and cash is not None and cash > 0:
        yrs = round(cash / abs(fcf), 1)
        runway = {
            "cash": cash, "cash_as_of": cash_as_of, "annual_burn": abs(fcf),
            "runway_years": yrs,
            "flag": (f"only {yrs}yr of cash at current burn — expect dilution or "
                     f"a raise soon" if yrs < 2 else f"{yrs}yr of cash at current burn"),
        }
        if (mrq_cash is not None and fy_cash is not None and fy_cash > 0
                and abs(mrq_cash - fy_cash) / fy_cash > 0.4):
            chg = (mrq_cash / fy_cash - 1) * 100
            runway["note"] = (f"cash moved {chg:+.0f}% since fiscal-year-end "
                              f"(likely a raise or heavy spend) — runway uses "
                              f"the fresher quarter-end figure")
    elif fcf is not None and fcf >= 0:
        runway = {"note": "free-cash-flow positive — runway not a concern"}

    return {"stage": stage, "profitable_years_in_window": ni_positive_years,
            "cash_runway": runway,
            "note": "for pre-revenue/early-stage names, judge on runway + dilution "
                    "+ milestones, not P/E or margins"}


def _normalized(rows: list[dict]) -> dict:
    """Multi-year averages + outlier-year detection.

    A single distorted year (e.g. a one-time tax charge or write-off) can make
    P/E or net margin look absurd. This reports the multi-year average margins
    and average EPS, and flags any year whose net margin deviates sharply from
    the average — a hint that a one-time item is distorting that year's ratios
    and you should lean on the normalized figure instead.
    """
    import statistics

    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # Outlier detection uses the MEDIAN as baseline, not the mean — a single
    # huge loss year would drag the mean and make every year look anomalous.
    net_vals = [r["net_margin_pct"] for r in rows if r.get("net_margin_pct") is not None]
    med_net = round(statistics.median(net_vals), 2) if net_vals else None
    avg_net = avg("net_margin_pct")
    outliers = []
    if med_net is not None:
        for r in rows:
            nm = r.get("net_margin_pct")
            if nm is not None and abs(nm - med_net) >= max(8.0, abs(med_net) * 0.6):
                outliers.append(
                    f"FY{r['fiscal_year']} net margin {nm:.1f}% vs {med_net:.1f}% "
                    f"median — likely a one-time item; use normalized figures")
    return {
        "avg_gross_margin_pct": avg("gross_margin_pct"),
        "avg_operating_margin_pct": avg("operating_margin_pct"),
        "avg_net_margin_pct": avg_net,
        "median_net_margin_pct": med_net,
        "avg_eps_diluted": avg("eps_diluted"),
        "avg_fcf_margin_pct": avg("fcf_margin_pct"),
        "outlier_years": outliers,
        "note": "averages over the available window; prefer these over a single "
                "distorted year when computing P/E or margin context",
    }


def _financial_trends(rows: list[dict]) -> list[str]:
    """rows are newest-first; describe direction over the window in plain words."""
    flags = []
    if len(rows) < 2:
        return flags
    oldest, newest = rows[-1], rows[0]

    def grew_every_year(key: str) -> bool:
        vals = [r[key] for r in reversed(rows) if r[key] is not None]
        return len(vals) >= 2 and all(b > a for a, b in zip(vals, vals[1:]))

    if grew_every_year("revenue"):
        flags.append(f"revenue grew every year ({len(rows)}-year window)")
    elif newest["revenue"] and oldest["revenue"]:
        chg = (newest["revenue"] / oldest["revenue"] - 1) * 100
        flags.append(f"revenue {'up' if chg >= 0 else 'down'} {abs(chg):.0f}% over the window")

    for key, label in (("net_margin_pct", "net margin"),
                       ("fcf_margin_pct", "FCF margin")):
        if newest[key] is not None and oldest[key] is not None:
            delta = newest[key] - oldest[key]
            if abs(delta) >= 2:
                verb = "expanded" if delta > 0 else "compressed"
                flags.append(f"{label} {verb} from {oldest[key]:.0f}% to {newest[key]:.0f}%")

    if newest["diluted_shares"] and oldest["diluted_shares"]:
        chg = (newest["diluted_shares"] / oldest["diluted_shares"] - 1) * 100
        if chg <= -1:
            flags.append(f"share count down {abs(chg):.0f}% — net buybacks (good for per-share value)")
        elif chg >= 3:
            flags.append(f"share count up {chg:.0f}% — dilution (erodes per-share value)")
    return flags


def get_quality_scores(ticker: str) -> dict:
    """Business-quality and financial-health scores for a single stock.

    Returns the Piotroski F-score (0-9; higher = healthier financials — measures
    profitability, leverage, and operating efficiency) and the Altman Z-score (a
    bankruptcy-risk gauge: >2.99 safe, 1.81-2.99 grey zone, <1.81 distress), plus
    core quality ratios: return on equity, return on invested capital, net
    debt/EBITDA (leverage), interest coverage, and current ratio. Includes
    plain-language interpretation flags.

    Sourced from Financial Modeling Prep. Returns {"error": ...} if unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not have_key():
        return _degraded(ticker, "no_key", "get_quality_scores")

    try:
        scores = fmp_get("financial-scores", symbol=ticker)
        km = fmp_get("key-metrics", symbol=ticker, period="annual", limit=1)
        ratios = fmp_get("ratios", symbol=ticker, period="annual", limit=1)
    except FMPError as exc:
        return _degraded(ticker, exc, "get_quality_scores")

    s = scores[0] if scores else {}
    m = km[0] if km else {}
    r = ratios[0] if ratios else {}

    piotroski = num(s.get("piotroskiScore"))
    altman = num(s.get("altmanZScore"))
    roe = _pct(m.get("returnOnEquity"))
    roic = _pct(m.get("returnOnInvestedCapital"))
    net_debt_ebitda = num(m.get("netDebtToEBITDA"))
    interest_cov = num(r.get("interestCoverageRatio"))
    current_ratio = num(r.get("currentRatio"))

    flags = []
    # Ratio-on-a-negative-denominator artifact: with negative EBITDA the
    # leverage ratio is meaningless garbage (QUBT printed 53.9x). Same limit=5
    # call the distress composite makes, so it's served from cache.
    try:
        income = fmp_get("income-statement", symbol=ticker, period="annual", limit=5)
    except FMPError:
        income = []
    ebitda = num(income[0].get("ebitda")) if income else None
    if net_debt_ebitda is not None and ebitda is not None and ebitda <= 0:
        flags.append("net debt/EBITDA not meaningful — EBITDA is negative; "
                     "judge leverage on cash runway and interest coverage instead")
        net_debt_ebitda = None
    if piotroski is not None:
        if piotroski >= 8:
            flags.append(f"Piotroski {piotroski:.0f}/9 — financially strong")
        elif piotroski <= 3:
            flags.append(f"Piotroski {piotroski:.0f}/9 — weak financials")
    # Distress handling: the sector-aware composite LEADS, Altman Z follows
    # with caveats. The classic Z-score is calibrated for manufacturers — it
    # rated Bed Bath & Beyond "safe" months before bankruptcy, and falsely
    # cried "bankruptcy risk" on merchant-power utilities (CEG/VST at Z~1.5)
    # whose balance sheets the composite read as fine. The scary headline must
    # never outrun the better signal.
    distress = _distress_composite(ticker)
    sector = distress.get("sector")
    z_reliable = sector not in {"Consumer Cyclical", "Consumer Defensive",
                                "Financial Services", "Real Estate",
                                "Utilities", "Energy"}
    if distress.get("available"):
        flags.append(f"distress composite (sector-aware, the primary solvency "
                     f"signal): {distress.get('severity')} "
                     f"({distress.get('red_flag_count')} red flag(s))")
    flags.extend(distress.get("flags", []))
    if altman is not None:
        zone = ("distress zone" if altman < 1.81
                else "grey zone" if altman < 2.99 else "safe zone")
        composite_benign = (distress.get("available")
                            and distress.get("severity") in ("none", "watch"))
        if altman < 1.81 and composite_benign:
            flags.append(f"Altman Z {altman:.1f} reads 'distress' but is NOT "
                         f"corroborated by the distress composite — for "
                         f"{sector or 'this sector'} the Z-score is a known "
                         f"false alarm; defer to the composite")
        elif not z_reliable:
            flags.append(f"Altman Z {altman:.1f} — {zone}; unreliable for "
                         f"{sector}, defer to the distress_composite")
        else:
            flags.append(f"Altman Z {altman:.1f} — {zone}")
    if roic is not None and roic >= 15:
        flags.append(f"ROIC {roic:.0f}% — high returns on capital (strong moat signal)")
    if net_debt_ebitda is not None and net_debt_ebitda > 3:
        flags.append(f"net debt/EBITDA {net_debt_ebitda:.1f}x — meaningfully leveraged")
    if interest_cov is not None and interest_cov < 3 and interest_cov > 0:
        flags.append(f"interest coverage {interest_cov:.1f}x — thin cushion on debt payments")

    return {
        "ticker": ticker,
        "piotroski_f_score": piotroski,
        "altman_z_score": altman,
        "return_on_equity_pct": roe,
        "return_on_invested_capital_pct": roic,
        "net_debt_to_ebitda": net_debt_ebitda,
        "interest_coverage": interest_cov,
        "current_ratio": current_ratio,
        "distress_composite": distress,
        "flags": flags,
        "source": "Financial Modeling Prep",
    }


def _distress_composite(ticker: str) -> dict:
    """Model-independent distress read — the signals that caught BBBY when
    Altman Z did not.

    Checks five hard red flags from the latest two fiscal years: negative
    equity, negative free cash flow, declining revenue, rising leverage, and a
    negative operating margin. Returns the count, a severity label, and
    plain-language flags. Sector-agnostic, so it works where Altman Z fails.
    """
    try:
        income = fmp_get("income-statement", symbol=ticker, period="annual", limit=5)
        cash = fmp_get("cash-flow-statement", symbol=ticker, period="annual", limit=2)
        bal = fmp_get("balance-sheet-statement", symbol=ticker, period="annual", limit=2)
        profile = fmp_get("profile", symbol=ticker)
    except FMPError:
        return {"available": False, "flags": [], "sector": None}

    if not income or not bal:
        return {"available": False, "flags": [], "sector": None}

    sector = (profile[0].get("sector") if profile else None)
    inc0 = income[0]
    inc1 = income[1] if len(income) > 1 else {}
    bal0 = bal[0]
    cash0 = cash[0] if cash else {}

    equity = num(bal0.get("totalStockholdersEquity"))
    fcf = num(cash0.get("freeCashFlow"))
    rev0, rev1 = num(inc0.get("revenue")), num(inc1.get("revenue"))
    op0 = num(inc0.get("operatingIncome"))
    de0 = (num(bal0.get("totalDebt")) / equity) if (equity and equity > 0) else None
    eq1 = num(bal[1].get("totalStockholdersEquity")) if len(bal) > 1 else None
    de1 = (num(bal[1].get("totalDebt")) / eq1) if (eq1 and eq1 > 0) else None

    # #4: a MATURE power utility's negative FCF is often hedging-collateral
    # timing, not cash burn — soften it. BUT only for real, revenue-generating
    # utilities: a pre-revenue developer tagged 'Utilities' (e.g. an SMR startup
    # like OKLO) that is burning cash IS in distress, so require material
    # revenue before applying the carve-out.
    is_mature_utility = (sector in {"Utilities", "Energy"}
                         and rev0 is not None and rev0 > 1_000_000_000)
    fcf_is_distress = not is_mature_utility

    flags = []
    if equity is not None and equity <= 0:
        flags.append("DISTRESS: negative shareholder equity (liabilities exceed assets)")
    if fcf is not None and fcf < 0:
        if fcf_is_distress:
            flags.append("DISTRESS: negative free cash flow (burning cash)")
        else:
            flags.append(f"negative FCF — but {sector} FCF swings are often "
                         f"hedging/collateral timing, not cash burn; verify")
    if rev0 is not None and rev1 is not None and rev0 < rev1:
        flags.append(f"revenue declining ({rev1/1e9:.1f}B → {rev0/1e9:.1f}B)")
    if de0 is not None and de1 is not None and de0 > de1 * 1.25 and de0 > 1:
        flags.append(f"leverage rising sharply (D/E {de1:.1f} → {de0:.1f})")
    if op0 is not None and op0 < 0:
        flags.append("operating losses (negative operating margin)")

    # Count only genuine distress (the softened utility-FCF note doesn't count).
    count = sum(1 for f in flags if not f.startswith("negative FCF — but"))
    severity = ("acute" if count >= 3 else "elevated" if count == 2
                else "watch" if count == 1 else "none")

    loss_type = _loss_type(income, sector, op0)
    return {
        "available": True,
        "sector": sector,
        "red_flag_count": count,
        "severity": severity,
        "loss_type": loss_type,
        "flags": flags,
    }


_CYCLICAL_SECTORS = {"Energy", "Industrials", "Basic Materials",
                     "Consumer Cyclical"}


def _loss_type(income: list[dict], sector: str | None, op0) -> str | None:
    """#6: when currently loss-making, is it CYCLICAL (recoverable) or STRUCTURAL?

    The backtest showed the distress signal conflated bombed-out cyclicals that
    recovered (GE, CCL → +200%) with structural decline (BBBY → bankruptcy).
    Separates them using multi-year profit history, the revenue trend, and
    sector cyclicality.
    """
    if op0 is None or op0 >= 0:
        return None  # not currently loss-making
    ops = [num(s.get("operatingIncome")) for s in income]
    revs = [num(s.get("revenue")) for s in income]  # newest-first
    profitable_prior = sum(1 for o in ops[1:] if o is not None and o > 0)
    # revenue falling every year across the window?
    rev_seq = [r for r in revs if r is not None]
    rev_declining_all = (len(rev_seq) >= 3
                         and all(a < b for a, b in zip(rev_seq, rev_seq[1:])))  # newest<older each step

    if rev_declining_all and profitable_prior <= 1:
        return ("structural_decline — revenue down every year + losses; treat the "
                "low price as a possible value trap, not a bargain")
    if profitable_prior >= 2 and (sector in _CYCLICAL_SECTORS):
        return ("cyclical_trough — historically profitable in a cyclical sector; "
                "the loss may be a recoverable trough, not terminal decline")
    if profitable_prior >= 2:
        return ("transitory_loss — profitable in most prior years; loss looks "
                "episodic rather than structural (verify the cause)")
    return "uncertain — mixed signals; inspect the cause of the loss directly"


def get_historical_valuation(ticker: str, years: int = 5) -> dict:
    """Is the stock's valuation high or low *for itself*? (default 5 yrs)

    The RANGE comes from annual fiscal-year-end multiples (the only history
    available); the CURRENT point is computed from the LIVE price, so the
    position-in-range never lags an intraday move. (Dogfooding bug: the old
    FY-end "current" still said "richly valued" after a crash.) Every P/E is
    labeled with its EPS basis — ttm (one-times included) vs normalized
    (median FY EPS) — and matches get_market_data / analyze_stocks exactly.

    Sourced from Financial Modeling Prep + live yfinance price.
    Returns {"error": ...} if unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not have_key():
        return _degraded(ticker, "no_key", "get_historical_valuation")

    n = max(2, min(int(years), 10))
    try:
        ratios = fmp_get("ratios", symbol=ticker, period="annual", limit=n)
        income = fmp_get("income-statement", symbol=ticker, period="annual", limit=n)
    except FMPError as exc:
        return _degraded(ticker, exc, "get_historical_valuation")
    if not ratios:
        return _degraded(ticker, "402", "get_historical_valuation")

    # Canonical live bases — same numbers every other tool reports.
    pe = pe_bases(ticker, n)
    info = get_info(ticker)
    live_current = {
        "pe": pe["ttm"],
        "ps": num(info.get("priceToSalesTrailing12Months")),
        "pb": num(info.get("priceToBook")),
    }

    field_map = {
        "pe": "priceToEarningsRatio",
        "ps": "priceToSalesRatio",
        "pb": "priceToBookRatio",
    }
    out = {}
    flags = []
    for label, field in field_map.items():
        series = [num(row.get(field)) for row in ratios]
        series = [v for v in series if v is not None and v > 0]
        if len(series) < 2:
            out[label] = None
            continue
        current = live_current.get(label)
        basis = "live price / TTM"
        if current is None or current <= 0:
            current = series[0]  # fall back to newest FY-end snapshot
            basis = "FY-end snapshot (live unavailable)"
        lo, hi, avg = min(series), max(series), sum(series) / len(series)
        position = (current - lo) / (hi - lo) if hi > lo else None
        out[label] = {
            "current": round(current, 2),
            "current_basis": basis,
            "min": round(lo, 2), "avg": round(avg, 2), "max": round(hi, 2),
            "position_in_range": round(position, 2) if position is not None else None,
            "years": len(series),
        }
        if position is not None:
            name = {"pe": "P/E", "ps": "P/S", "pb": "P/B"}[label]
            if position >= 0.8:
                flags.append(f"{name} near the top of its {len(series)}-yr range (richly valued vs own history)")
            elif position <= 0.2:
                flags.append(f"{name} near the bottom of its {len(series)}-yr range (cheap vs own history)")

    max_years = max((node["years"] for node in out.values() if node), default=0)
    if 0 < max_years < 3:
        flags.append(f"only {max_years} fiscal year(s) of multiple history — "
                     f"vs-own-history positions are weak context (recent "
                     f"listing); lean on peer/basket comparison instead")

    # Normalized P/E — live price / median FY EPS (canonical basis). Guards
    # against the transitory-earnings trap (PFE looked cheap on COVID-windfall
    # EPS, then fell 48%). If TTM P/E is far below normalized, the "cheap" is
    # likely a peak-earnings illusion.
    eps_series = [num(s.get("epsDiluted")) for s in (income or [])]   # newest-first
    eps_series = [e for e in eps_series if e is not None]
    rev_series = [num(s.get("revenue")) for s in (income or [])]
    rev_series = [r for r in rev_series if r is not None]
    norm_pe, ttm_pe = pe["normalized"], pe["ttm"]
    normalized = {}
    if eps_series and (norm_pe or ttm_pe):
        # Is the earnings elevation growth-driven or a transitory peak? Use
        # revenue trend as the discriminator: steadily rising revenue backing a
        # rising-to-peak EPS = secular GROWTH (don't cry "unsustainable"); EPS
        # elevated WITHOUT durable revenue growth = likely transitory peak.
        rev_chrono = list(reversed(rev_series))
        rev_up = (len(rev_chrono) >= 3
                  and sum(1 for a, b in zip(rev_chrono, rev_chrono[1:]) if b > a)
                  >= (len(rev_chrono) - 1) * 0.75)
        latest_fy_eps = eps_series[0]
        eps_is_peak = latest_fy_eps >= max(eps_series)
        secular_growth = rev_up and eps_is_peak

        normalized = {"pe_normalized": norm_pe,
                      "pe_ttm": ttm_pe,
                      "eps_normalized_median": pe["eps"]["normalized_median"],
                      "eps_ttm": pe["eps"]["ttm"],
                      "basis": "live price; normalized = median FY diluted EPS "
                               f"over {pe['eps']['window_years']} yrs, "
                               "ttm = trailing-12mo reported (incl. one-times)",
                      "earnings_pattern": "secular-growth" if secular_growth
                      else "elevated-vs-median" if (norm_pe and ttm_pe
                                                    and norm_pe > ttm_pe * 1.5)
                      else "stable"}
        if norm_pe and ttm_pe and norm_pe > ttm_pe * 1.5:
            if secular_growth:
                flags.append(f"normalized P/E ({norm_pe}) >> ttm ({ttm_pe}), "
                             f"but EPS rose on a steady revenue uptrend — likely GROWTH, "
                             f"not a transitory peak; judge on forward P/E. (Caveat: a "
                             f"one-time DEMAND surge, e.g. COVID, can mimic this — verify "
                             f"the revenue growth is durable, not a single-event spike.)")
            else:
                flags.append(f"transitory-earnings risk: ttm P/E {ttm_pe} "
                             f"looks cheap but normalized P/E is {norm_pe}, and revenue "
                             f"is NOT in a steady uptrend — current earnings may be a "
                             f"peak that reverts (verify the cause)")

    # Flag P/S when revenue is immaterial (P/S becomes meaningless).
    ps_node = out.get("ps")
    if ps_node and ps_node.get("current", 0) > 50:
        flags.append("P/S is extreme (>50) — revenue is immaterial relative to "
                     "market cap; treat P/S as not meaningful here")

    return {"ticker": ticker, "multiples": out, "normalized": normalized,
            "flags": flags,
            "method": "range from annual fiscal-year-end multiples (directional, "
                      "not a precise percentile); CURRENT point from the live "
                      "price so it reflects today's move",
            "peer_context": "for cross-sectional context (sector-relative, not just "
                            "vs own history), also call get_peers",
            "source": "Financial Modeling Prep + live yfinance price"}


def get_peers(ticker: str, limit: int = 5) -> dict:
    """Compare a stock's valuation against similar companies.

    Returns up to `limit` peer companies, each with market cap and current P/E
    and P/S, alongside the target stock's own multiples, so you can see whether
    it's cheap or expensive relative to its group. Includes the peer-group
    median P/E and P/S for a quick reference point.

    Sourced from Financial Modeling Prep. Returns {"error": ...} if unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not have_key():
        return _degraded(ticker, "no_key", "get_peers")

    # Peers = same industry, nearest market cap, via the screener. FMP's
    # stock-peers endpoint returned Bilibili and Duolingo as quantum-hardware
    # peers and MicroStrategy as a design-software peer — classification
    # nonsense. The screener route is deterministic; stock-peers remains the
    # fallback when industry/mcap are missing.
    try:
        profile = fmp_get("profile", symbol=ticker)
    except FMPError as exc:
        return _degraded(ticker, exc, "get_peers")
    prof = profile[0] if profile else {}
    industry = prof.get("industry")
    mcap = num(prof.get("marketCap"))

    peer_rows: list[tuple] = []
    if industry and mcap and mcap > 0:
        try:
            rows = fmp_get("company-screener", industry=industry,
                           marketCapMoreThan=int(mcap * 0.05),
                           marketCapLowerThan=int(mcap * 20),
                           isEtf="false", isActivelyTrading="true", limit=100)
        except FMPError:
            rows = []
        import math
        cands = []
        for r in rows if isinstance(rows, list) else []:
            sym = str(r.get("symbol") or "").upper()
            pcap = num(r.get("marketCap"))
            exch = str(r.get("exchange") or "").upper()
            if (sym and sym != ticker and pcap and pcap > 0
                    and sym.isalpha() and len(sym) <= 5
                    and any(x in exch for x in ("NASDAQ", "NYSE", "NEW YORK",
                                                "AMEX", "AMERICAN"))):
                cands.append((abs(math.log(pcap / mcap)), sym, pcap,
                              r.get("companyName")))
        cands.sort()
        peer_rows = [(sym, pcap, name)
                     for _, sym, pcap, name in cands[: max(1, int(limit))]]

    if not peer_rows:
        try:
            peers = fmp_get("stock-peers", symbol=ticker)
        except FMPError as exc:
            return _degraded(ticker, exc, "get_peers")
        peer_rows = [(p.get("symbol"),
                      num(p.get("mktCap") or p.get("marketCap")),
                      p.get("companyName"))
                     for p in (peers or []) if p.get("symbol")][: max(1, int(limit))]
    if not peer_rows:
        return _degraded(ticker, "402", "get_peers")

    def multiples_for(sym: str) -> dict:
        try:
            r = fmp_get("ratios", symbol=sym, period="annual", limit=1)
        except FMPError:
            return {"pe": None, "ps": None}
        row = r[0] if r else {}
        return {"pe": num(row.get("priceToEarningsRatio")),
                "ps": num(row.get("priceToSalesRatio"))}

    target = multiples_for(ticker)
    table = []
    for sym, pcap, name in peer_rows:
        mult = multiples_for(sym)
        table.append({
            "symbol": sym,
            "name": name,
            "market_cap": pcap,
            "pe": round(mult["pe"], 2) if mult["pe"] else None,
            "ps": round(mult["ps"], 2) if mult["ps"] else None,
        })

    def median(vals):
        vals = sorted(v for v in vals if v is not None and v > 0)
        if not vals:
            return None
        mid = len(vals) // 2
        return round((vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2), 2)

    return {
        "ticker": ticker,
        "target_pe": round(target["pe"], 2) if target["pe"] else None,
        "target_ps": round(target["ps"], 2) if target["ps"] else None,
        "peer_median_pe": median([p["pe"] for p in table]),
        "peer_median_ps": median([p["ps"] for p in table]),
        "peers": table,
        "method": "same industry, nearest market cap (screener-based)",
        "source": "Financial Modeling Prep",
    }


def reverse_dcf(ticker: str, discount_rate: float = 0.10,
                terminal_growth: float = 0.025, years: int = 10) -> dict:
    """What free-cash-flow growth rate is the current price already pricing in?

    Instead of guessing a fair value (a forward DCF launders your own growth
    assumption into a number), this solves the inverse: what constant annual
    FCF growth over `years` (default 10), with terminal growth
    `terminal_growth` (2.5%) and discount rate `discount_rate` (10%), justifies
    today's market cap? The output is *implied* growth — the human judges
    plausibility.

    BASE-YEAR GUARD (dogfooding scar): a one-year FCF collapse/spike makes the
    latest-FCF answer meaningless (a name once showed "priced for 50% growth"
    purely because its base year cratered). So this anchors on the NORMALIZED
    (median multi-year) FCF whenever the latest year deviates sharply from it,
    and reports the implied growth on BOTH bases so the distortion is visible.

    Simplifying assumptions (stated so the number isn't over-trusted): FCF
    treated as equity cash flow; market cap (live) as the value to match; share
    count and net cash/debt changes ignored. Directional, not precise.

    Sourced from Financial Modeling Prep + live market cap.
    Returns {"error": ...} if unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not have_key():
        return _degraded(ticker, "no_key", "reverse_dcf")
    if discount_rate <= terminal_growth:
        return {"error": "discount_rate must exceed terminal_growth"}

    fcf = fcf_bases(ticker, years=5)
    market_cap = live_market_cap(ticker)
    if market_cap is None:
        try:
            profile = fmp_get("profile", symbol=ticker)
            market_cap = num(profile[0].get("marketCap")) if profile else None
        except FMPError as exc:
            return _degraded(ticker, exc, "reverse_dcf")

    latest = fcf["latest_fy"]
    normalized = fcf["normalized_median"]
    prior_med = fcf.get("prior_years_median")
    if market_cap is None or latest is None:
        return {"error": f"missing market cap or FCF for {ticker}", "ticker": ticker,
                "partial": {"market_cap": market_cap, "free_cash_flow": latest}}

    # Anchor base: when the latest year is distorted, prefer the prior-years
    # median (the baseline the distortion was measured against), then the
    # whole-series median; otherwise the latest year.
    base_label, base_fcf = "latest_fy", latest
    if fcf["distorted"]:
        if prior_med and prior_med > 0:
            base_label, base_fcf = "prior_years_median", prior_med
        elif normalized and normalized > 0:
            base_label, base_fcf = "normalized_median", normalized
    if base_fcf is None or base_fcf <= 0:
        return {"error": f"{ticker} has non-positive free cash flow on every "
                         f"base (latest {latest:,.0f}) — reverse DCF does not apply",
                "ticker": ticker, "fcf_bases": fcf}

    n = max(1, min(int(years), 30))

    def implied_growth_for(fcf0: float) -> float | None:
        """Bisect for the growth rate that makes PV(fcf0 growing) == market cap.
        None means even >100%/yr can't justify the price."""
        def pv(g: float) -> float:
            total, cf = 0.0, fcf0
            for t in range(1, n + 1):
                cf = fcf0 * ((1 + g) ** t)
                total += cf / ((1 + discount_rate) ** t)
            terminal = cf * (1 + terminal_growth) / (discount_rate - terminal_growth)
            return total + terminal / ((1 + discount_rate) ** n)

        lo, hi = -0.50, 1.00
        if pv(hi) < market_cap:
            return None
        implied = None
        for _ in range(100):
            mid = (lo + hi) / 2
            if pv(mid) > market_cap:
                hi = mid
            else:
                lo = mid
            implied = mid
            if hi - lo < 1e-6:
                break
        return implied

    results = {}
    for label, f in (("latest_fy", latest), ("prior_years_median", prior_med),
                     ("normalized_median", normalized)):
        if f is not None and f > 0:
            g = implied_growth_for(f)
            results[label] = round(g * 100, 1) if g is not None else None
        else:
            results[label] = "n/a (non-positive FCF on this base)"

    implied_pct = results.get(base_label)
    flags = []
    if fcf.get("distorted"):
        flags.append(fcf.get("note", "latest-FY FCF base is distorted"))
        if base_label != "latest_fy":
            flags.append(f"anchoring on {base_label} FCF; the latest-FCF implied "
                         f"growth ({results.get('latest_fy')}%) is shown for "
                         f"transparency but should not drive the judgment")
        else:
            flags.append("no positive normalized base exists (prior-year FCF "
                         "negative), so the latest year is the only usable base "
                         "— treat the implied growth as unreliable and judge on "
                         "the FCF trajectory instead")
    if fcf.get("volatile_series"):
        flags.append(fcf.get("volatility_note", "FCF series is volatile"))

    if implied_pct is None:
        interpretation = ("even >100%/yr FCF growth can't justify the price "
                          "under these assumptions — implied growth is extreme")
        verdict = None
    else:
        verdict = ("modest — easily achievable for a healthy company" if implied_pct < 5
                   else "moderate — plausible for a solid grower" if implied_pct < 12
                   else "high — requires sustained strong execution" if implied_pct < 20
                   else "very high — priced for exceptional, prolonged growth")
        interpretation = (f"The market is pricing in ~{implied_pct}% annual FCF "
                          f"growth for {n} years (on the {base_label} base). "
                          f"That is {verdict}.")

    return {
        "ticker": ticker,
        "implied_fcf_growth_pct": implied_pct,
        "fcf_base_used": base_label,
        "implied_growth_by_base_pct": results,
        "interpretation": interpretation,
        "market_cap": market_cap,
        "fcf_bases": {"latest_fy": latest, "prior_years_median": prior_med,
                      "normalized_median": normalized,
                      "deviation_pct": fcf.get("deviation_pct"),
                      "by_year": fcf.get("by_year")},
        "flags": flags,
        "assumptions": {
            "discount_rate_pct": discount_rate * 100,
            "terminal_growth_pct": terminal_growth * 100,
            "explicit_years": n,
            "simplification": "FCF treated as equity cash flow; share count and "
                              "net debt changes ignored; market cap is live",
        },
        "source": "Financial Modeling Prep + live yfinance market cap",
    }
