"""Fundamentals, quality, historical valuation, peers, reverse-DCF (FMP-backed).

These are the v2 tools. They use Financial Modeling Prep's /stable API for
clean multi-year financials the free market-data source can't supply reliably.
All default to a 3-year window. Every tool fails loudly if FMP is unavailable.
"""

from __future__ import annotations

from .common import get_info, normalize_ticker, num
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
    cash_eq = num(bal.get("cashAndShortTermInvestments")) or num(bal.get("cashAndCashEquivalents"))
    stage = _company_stage(rows, cash_eq)
    return {"ticker": ticker, "years": len(rows), "annual": rows,
            "trends": trends, "normalized": normalized,
            "company_stage": stage,
            "source": "Financial Modeling Prep"}


def _company_stage(rows: list[dict], cash: float | None) -> dict:
    """Classify lifecycle stage and (for cash-burners) the cash runway.

    Pre-revenue/early-stage names can't be judged on P/E or margins — the
    decisive question is how long the cash lasts. This returns the stage plus,
    when free cash flow is negative, runway = cash / annual burn (in years).
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

    runway = None
    if fcf is not None and fcf < 0 and cash is not None and cash > 0:
        yrs = round(cash / abs(fcf), 1)
        runway = {
            "cash": cash, "annual_burn": abs(fcf), "runway_years": yrs,
            "flag": (f"only {yrs}yr of cash at current burn — expect dilution or "
                     f"a raise soon" if yrs < 2 else f"{yrs}yr of cash at current burn"),
        }
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
    if piotroski is not None:
        if piotroski >= 8:
            flags.append(f"Piotroski {piotroski:.0f}/9 — financially strong")
        elif piotroski <= 3:
            flags.append(f"Piotroski {piotroski:.0f}/9 — weak financials")
    # Sector-aware Altman handling: the classic Z-score is calibrated for
    # manufacturers and OVER-scores asset-light/lease-heavy retailers and
    # financials (it rated Bed Bath & Beyond "safe" months before bankruptcy).
    # Always pair it with the model-independent distress composite below.
    distress = _distress_composite(ticker)
    sector = distress.get("sector")
    z_reliable = sector not in {"Consumer Cyclical", "Consumer Defensive",
                                "Financial Services", "Real Estate"}
    if altman is not None:
        zone = ("distress zone (elevated bankruptcy risk)" if altman < 1.81
                else "grey zone" if altman < 2.99 else "safe zone")
        caveat = "" if z_reliable else (
            f" — but Altman Z is unreliable for {sector}; "
            f"defer to the distress_composite")
        flags.append(f"Altman Z {altman:.1f} — {zone}{caveat}")
    flags.extend(distress.get("flags", []))
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


def get_historical_valuation(ticker: str, years: int = 3) -> dict:
    """Is the stock's current valuation high or low *for itself*? (default 3 yrs)

    Fills the gap the basic get_valuation_context could not: it pulls the
    annual P/E, P/S, and P/B for the past `years` and reports each multiple's
    range (min/avg/max) plus the most recent value's position within that range,
    with a plain-language verdict (e.g. "P/E near the top of its 3-year range").

    Note: based on annual fiscal-year-end multiples (a handful of points), so
    treat it as directional context, not a precise percentile.

    Sourced from Financial Modeling Prep. Returns {"error": ...} if unavailable.
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
        profile = fmp_get("profile", symbol=ticker)
    except FMPError as exc:
        return _degraded(ticker, exc, "get_historical_valuation")
    if not ratios:
        return _degraded(ticker, "402", "get_historical_valuation")

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
        current = series[0]  # newest first
        lo, hi, avg = min(series), max(series), sum(series) / len(series)
        position = (current - lo) / (hi - lo) if hi > lo else None
        out[label] = {
            "current": round(current, 2),
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

    # #7: normalized P/E — current price / median EPS over the window. Guards
    # against the transitory-earnings trap (PFE looked cheap on COVID-windfall
    # EPS, then fell 48%). If trailing P/E is far below normalized, the "cheap"
    # is likely a peak-earnings illusion.
    import statistics
    eps_series = [num(s.get("epsDiluted")) for s in (income or [])]   # newest-first
    eps_series = [e for e in eps_series if e is not None]
    rev_series = [num(s.get("revenue")) for s in (income or [])]
    rev_series = [r for r in rev_series if r is not None]
    price = num(profile[0].get("price")) if profile else None
    normalized = {}
    if price and eps_series:
        med_eps = statistics.median(eps_series)
        trailing_eps = eps_series[0]
        norm_pe = round(price / med_eps, 1) if med_eps > 0 else None
        trailing_pe = round(price / trailing_eps, 1) if trailing_eps > 0 else None

        # Is the earnings elevation growth-driven or a transitory peak? Use
        # revenue trend as the discriminator: steadily rising revenue backing a
        # rising-to-peak EPS = secular GROWTH (don't cry "unsustainable"); EPS
        # elevated WITHOUT durable revenue growth = likely transitory peak.
        rev_chrono = list(reversed(rev_series))
        rev_up = (len(rev_chrono) >= 3
                  and sum(1 for a, b in zip(rev_chrono, rev_chrono[1:]) if b > a)
                  >= (len(rev_chrono) - 1) * 0.75)
        eps_is_peak = trailing_eps >= max(eps_series)
        secular_growth = rev_up and eps_is_peak

        normalized = {"normalized_pe_on_median_eps": norm_pe,
                      "trailing_pe": trailing_pe,
                      "median_eps": round(med_eps, 2),
                      "trailing_eps": round(trailing_eps, 2),
                      "earnings_pattern": "secular-growth" if secular_growth
                      else "elevated-vs-median" if (norm_pe and trailing_pe
                                                    and norm_pe > trailing_pe * 1.5)
                      else "stable"}
        if norm_pe and trailing_pe and norm_pe > trailing_pe * 1.5:
            if secular_growth:
                flags.append(f"normalized P/E ({norm_pe}) >> trailing ({trailing_pe}), "
                             f"but EPS rose on a steady revenue uptrend — likely GROWTH, "
                             f"not a transitory peak; judge on forward P/E. (Caveat: a "
                             f"one-time DEMAND surge, e.g. COVID, can mimic this — verify "
                             f"the revenue growth is durable, not a single-event spike.)")
            else:
                flags.append(f"transitory-earnings risk: trailing P/E {trailing_pe} "
                             f"looks cheap but normalized P/E is {norm_pe}, and revenue "
                             f"is NOT in a steady uptrend — current earnings may be a "
                             f"peak that reverts (verify the cause)")

    # #3: flag P/S when revenue is immaterial (P/S becomes meaningless).
    ps_node = out.get("ps")
    if ps_node and ps_node.get("current", 0) > 50:
        flags.append("P/S is extreme (>50) — revenue is immaterial relative to "
                     "market cap; treat P/S as not meaningful here")

    return {"ticker": ticker, "multiples": out, "normalized": normalized,
            "flags": flags,
            "method": "annual fiscal-year-end multiples; directional, not a precise percentile",
            "peer_context": "for cross-sectional context (sector-relative, not just "
                            "vs own history), also call get_peers",
            "source": "Financial Modeling Prep"}


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

    try:
        peers = fmp_get("stock-peers", symbol=ticker)
    except FMPError as exc:
        return _degraded(ticker, exc, "get_peers")
    if not peers:
        return _degraded(ticker, "402", "get_peers")

    peer_symbols = [p.get("symbol") for p in peers if p.get("symbol")][: max(1, int(limit))]

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
    for p in peers:
        sym = p.get("symbol")
        if sym not in peer_symbols:
            continue
        mult = multiples_for(sym)
        table.append({
            "symbol": sym,
            "name": p.get("companyName"),
            "market_cap": num(p.get("mktCap") or p.get("marketCap")),
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
        "source": "Financial Modeling Prep",
    }


def reverse_dcf(ticker: str, discount_rate: float = 0.10,
                terminal_growth: float = 0.025, years: int = 10) -> dict:
    """What free-cash-flow growth rate is the current price already pricing in?

    Instead of guessing a fair value (a forward DCF launders your own growth
    assumption into a number), this solves the inverse: given the current market
    cap and latest free cash flow, what constant annual FCF growth over `years`
    (default 10), plus a terminal growth of `terminal_growth` (default 2.5%) and
    discount rate `discount_rate` (default 10%), is required to justify today's
    price? The output is the *implied* growth rate — the human then judges
    whether that growth is plausible.

    Simplifying assumptions (stated so the number isn't over-trusted): treats
    free cash flow as cash to equity holders and market cap as the value to
    match; ignores changing share count and net cash/debt nuances. Directional,
    not a precise valuation. Fails if FCF is non-positive (model doesn't apply).

    Sourced from Financial Modeling Prep. Returns {"error": ...} if unavailable.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}
    if not have_key():
        return _degraded(ticker, "no_key", "reverse_dcf")
    if discount_rate <= terminal_growth:
        return {"error": "discount_rate must exceed terminal_growth"}

    try:
        profile = fmp_get("profile", symbol=ticker)
        cash = fmp_get("cash-flow-statement", symbol=ticker, period="annual", limit=1)
    except FMPError as exc:
        return _degraded(ticker, exc, "reverse_dcf")

    prof = profile[0] if profile else {}
    market_cap = num(prof.get("marketCap"))
    fcf = num(cash[0].get("freeCashFlow")) if cash else None

    if market_cap is None or fcf is None:
        return {"error": f"missing market cap or FCF for {ticker}", "ticker": ticker,
                "partial": {"market_cap": market_cap, "free_cash_flow": fcf}}
    if fcf <= 0:
        return {"error": f"{ticker} has non-positive free cash flow "
                         f"({fcf:,.0f}) — reverse DCF does not apply",
                "ticker": ticker, "free_cash_flow": fcf}

    n = max(1, min(int(years), 30))

    def pv_for_growth(g: float) -> float:
        total = 0.0
        cf = fcf
        for t in range(1, n + 1):
            cf = fcf * ((1 + g) ** t)
            total += cf / ((1 + discount_rate) ** t)
        terminal = cf * (1 + terminal_growth) / (discount_rate - terminal_growth)
        total += terminal / ((1 + discount_rate) ** n)
        return total

    # Bisection for the growth rate that makes PV == market cap.
    lo, hi = -0.50, 1.00
    if pv_for_growth(hi) < market_cap:
        return {"ticker": ticker,
                "note": "even >100%/yr FCF growth can't justify the price under "
                        "these assumptions — implied growth is extreme",
                "implied_growth_pct": None, "market_cap": market_cap,
                "latest_fcf": fcf, "assumptions": {
                    "discount_rate_pct": discount_rate * 100,
                    "terminal_growth_pct": terminal_growth * 100, "years": n}}
    implied = None
    for _ in range(100):
        mid = (lo + hi) / 2
        if pv_for_growth(mid) > market_cap:
            hi = mid
        else:
            lo = mid
        implied = mid
        if hi - lo < 1e-6:
            break

    implied_pct = round(implied * 100, 1)
    verdict = ("modest — easily achievable for a healthy company" if implied_pct < 5
               else "moderate — plausible for a solid grower" if implied_pct < 12
               else "high — requires sustained strong execution" if implied_pct < 20
               else "very high — priced for exceptional, prolonged growth")

    return {
        "ticker": ticker,
        "implied_fcf_growth_pct": implied_pct,
        "interpretation": f"The market is pricing in ~{implied_pct}% annual FCF "
                          f"growth for {n} years. That is {verdict}.",
        "market_cap": market_cap,
        "latest_fcf": fcf,
        "assumptions": {
            "discount_rate_pct": discount_rate * 100,
            "terminal_growth_pct": terminal_growth * 100,
            "explicit_years": n,
            "simplification": "FCF treated as equity cash flow; share count and "
                              "net debt changes ignored",
        },
        "source": "Financial Modeling Prep",
    }
