"""Point-in-time (PIT) reconstruction engine for the judgment-quality backtest.

Pulls authoritative US fundamentals from SEC EDGAR's XBRL companyfacts API and
prices from yfinance, then reconstructs what each metric looked like *as of* a
given date — using only data filed on or before that date (no look-ahead).

Fundamentals use the latest ANNUAL (10-K) figures known as of the date. Annual
avoids fragile trailing-twelve-month stitching; the tradeoff is up to ~15 months
of staleness, which we note in every snapshot. Balance-sheet leverage is also
shown from the most recent quarterly instant when fresher.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import requests
import yfinance as yf

UA = {"User-Agent": "arkspeedo@gmail.com"}
CACHE = Path(__file__).resolve().parent / "_cache"
CACHE.mkdir(exist_ok=True)

CIKS = {
    "CSCO": "0000858877",
    "NVDA": "0001045810",
    "META": "0001326801",
    "BBBY": "0000886158",
    # Quantum
    "IONQ": "0001824920", "RGTI": "0001838359", "QBTS": "0001907982",
    "QUBT": "0001758009",
    # Nuclear / uranium (US filers only — Cameco/NexGen file foreign forms)
    "CEG": "0001868275", "VST": "0001692819", "OKLO": "0001849056",
    "SMR": "0001822966", "LEU": "0001065059", "UEC": "0001334933",
}

_ticker_map: dict | None = None


def _resolve_cik(ticker: str) -> str | None:
    """CIK from the hardcoded map, else SEC's company_tickers.json (cached)."""
    if ticker in CIKS:
        return CIKS[ticker]
    global _ticker_map
    if _ticker_map is None:
        mp = CACHE / "company_tickers.json"
        if mp.exists():
            raw = json.loads(mp.read_text())
        else:
            raw = requests.get("https://www.sec.gov/files/company_tickers.json",
                               headers=UA, timeout=30).json()
            mp.write_text(json.dumps(raw))
        _ticker_map = {str(e["ticker"]).upper(): str(e["cik_str"]).zfill(10)
                       for e in raw.values()}
    return _ticker_map.get(ticker.upper())

# Concept tag fallbacks (companies tag the same line item differently).
REVENUE = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
           "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]
GROSS = ["GrossProfit"]
OP_INCOME = ["OperatingIncomeLoss"]
NET_INCOME = ["NetIncomeLoss"]
EPS_DIL = ["EarningsPerShareDiluted"]
SHARES_DIL = ["WeightedAverageNumberOfDilutedSharesOutstanding",
              "WeightedAverageNumberOfSharesOutstandingBasic"]
CFO = ["NetCashProvidedByUsedInOperatingActivities",
       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment",
         "PaymentsToAcquireProductiveAssets"]
ASSETS = ["Assets"]
LIABS = ["Liabilities"]
EQUITY = ["StockholdersEquity",
          "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
CUR_ASSETS = ["AssetsCurrent"]
CUR_LIABS = ["LiabilitiesCurrent"]
RET_EARN = ["RetainedEarningsAccumulatedDeficit"]
LT_DEBT = ["LongTermDebtNoncurrent", "LongTermDebt"]
CASH = ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"]


def companyfacts(ticker: str) -> dict:
    cik = _resolve_cik(ticker)
    if cik is None:
        raise ValueError(f"no CIK for {ticker}")
    cache_file = CACHE / f"{ticker}_facts.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                     headers=UA, timeout=60)
    r.raise_for_status()
    data = r.json()
    cache_file.write_text(json.dumps(data))
    return data


def _facts_for(gaap: dict, names: list[str]) -> list[dict]:
    """All facts merged across EVERY listed tag variant that exists.

    Companies switch XBRL tags over time (e.g. NVDA's revenue), so we must merge
    all fallback tags and let the date-based selection pick the genuinely latest
    fact — returning only the first existing tag truncates the series.
    """
    out = []
    for n in names:
        node = gaap.get(n)
        if not node:
            continue
        for unit, items in node.get("units", {}).items():
            for it in items:
                out.append({**it, "_unit": unit, "_tag": n})
    return out


def _annual_value(gaap: dict, names: list[str], as_of: date, kind: str):
    """Latest annual value known as of `as_of`.

    kind="duration": full-year flow (revenue, net income) — period span ~330-400d.
    kind="instant": balance-sheet item — has only 'end'.
    Picks the fact with the most recent period-end whose 'filed' <= as_of; ties
    broken by latest filing (captures the as-originally-reported-then value).
    """
    facts = _facts_for(gaap, names)
    candidates = []
    for f in facts:
        filed = f.get("filed")
        end = f.get("end")
        if not filed or not end:
            continue
        if datetime.strptime(filed, "%Y-%m-%d").date() > as_of:
            continue  # not yet known
        if kind == "duration":
            start = f.get("start")
            if not start:
                continue
            span = (datetime.strptime(end, "%Y-%m-%d").date()
                    - datetime.strptime(start, "%Y-%m-%d").date()).days
            if not (330 <= span <= 400):  # full fiscal year only
                continue
        candidates.append(f)
    if not candidates:
        return None, None
    # Most recent fiscal period end, then latest filing.
    candidates.sort(key=lambda f: (f["end"], f["filed"]))
    best = candidates[-1]
    return best.get("val"), best.get("end")


def _quarter_instant(gaap: dict, names: list[str], as_of: date):
    """Latest balance-sheet (instant) value known as of date, any period."""
    return _annual_value(gaap, names, as_of, kind="instant")


def fundamentals_as_of(ticker: str, as_of: date) -> dict:
    """Reconstruct annual fundamentals + ratios known as of `as_of`."""
    gaap = companyfacts(ticker)["facts"]["us-gaap"]

    rev, fy_end = _annual_value(gaap, REVENUE, as_of, "duration")
    gross, _ = _annual_value(gaap, GROSS, as_of, "duration")
    op, _ = _annual_value(gaap, OP_INCOME, as_of, "duration")
    ni, _ = _annual_value(gaap, NET_INCOME, as_of, "duration")
    eps, _ = _annual_value(gaap, EPS_DIL, as_of, "duration")
    shares, _ = _annual_value(gaap, SHARES_DIL, as_of, "duration")
    cfo, _ = _annual_value(gaap, CFO, as_of, "duration")
    capex, _ = _annual_value(gaap, CAPEX, as_of, "duration")

    # Balance sheet: most recent instant known (may be fresher than the 10-K).
    assets, bs_end = _quarter_instant(gaap, ASSETS, as_of)
    liabs, _ = _quarter_instant(gaap, LIABS, as_of)
    equity, _ = _quarter_instant(gaap, EQUITY, as_of)
    cur_a, _ = _quarter_instant(gaap, CUR_ASSETS, as_of)
    cur_l, _ = _quarter_instant(gaap, CUR_LIABS, as_of)
    ret_earn, _ = _quarter_instant(gaap, RET_EARN, as_of)
    debt, _ = _quarter_instant(gaap, LT_DEBT, as_of)
    cash, _ = _quarter_instant(gaap, CASH, as_of)

    fcf = (cfo - capex) if (cfo is not None and capex is not None) else None

    def pct(n, d):
        return round(n / d * 100, 2) if (n is not None and d and d != 0) else None

    return {
        "fiscal_year_end": fy_end,
        "balance_sheet_end": bs_end,
        "revenue": rev,
        "gross_margin_pct": pct(gross, rev),
        "operating_margin_pct": pct(op, rev),
        "net_margin_pct": pct(ni, rev),
        "net_income": ni,
        "eps_diluted": eps,
        "diluted_shares": shares,
        "operating_cash_flow": cfo,
        "capex": capex,
        "free_cash_flow": fcf,
        "fcf_margin_pct": pct(fcf, rev),
        "total_assets": assets,
        "total_liabilities": liabs,
        "equity": equity,
        "current_ratio": round(cur_a / cur_l, 2) if (cur_a and cur_l) else None,
        "debt_to_equity": round(debt / equity, 2) if (debt and equity and equity > 0) else None,
        "negative_equity": (equity is not None and equity <= 0),
        "return_on_equity_pct": pct(ni, equity) if (equity and equity > 0) else None,
        "long_term_debt": debt,
        "cash": cash,
        "retained_earnings": ret_earn,
        "_raw": {"ebit": op, "working_capital":
                 (cur_a - cur_l) if (cur_a is not None and cur_l is not None) else None},
    }


_price_cache: dict[str, object] = {}


def _history(ticker: str):
    if ticker not in _price_cache:
        import warnings
        warnings.filterwarnings("ignore")
        _price_cache[ticker] = yf.Ticker(ticker).history(
            period="max", auto_adjust=False)
    return _price_cache[ticker]


def price_metrics_as_of(ticker: str, as_of: date) -> dict:
    """Raw price (for valuation), plus trailing total-return technicals as-of date.

    Uses raw Close for the spot price and Adj Close (dividends/splits reinvested)
    for returns/drawdown/volatility. Everything is sliced to on-or-before as_of —
    no future bars.
    """
    import numpy as np
    hist = _history(ticker)
    cutoff = __import__("pandas").Timestamp(as_of, tz=hist.index.tz)
    win = hist[hist.index <= cutoff]
    if len(win) < 30:
        return {"price": None, "note": "insufficient price history before date"}

    raw = win["Close"].dropna()
    adj = win["Adj Close"].dropna()
    price = float(raw.iloc[-1])

    def trailing_return(days):
        if len(adj) <= days:
            return None
        return round((adj.iloc[-1] / adj.iloc[-(days + 1)] - 1) * 100, 1)

    # Max drawdown over trailing ~2y of adjusted prices.
    look = adj.tail(504)
    dd = float((look / look.cummax() - 1).min()) * 100

    logret = np.log(adj / adj.shift(1)).dropna().tail(252)
    vol = float(logret.std() * np.sqrt(252) * 100) if len(logret) > 30 else None

    # RSI(14) and SMA position on raw close.
    delta = raw.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = float(100 - 100/(1 + gain.iloc[-1]/loss.iloc[-1])) if loss.iloc[-1] else 100.0
    sma200 = float(raw.tail(200).mean()) if len(raw) >= 200 else None

    return {
        "as_of_trading_day": win.index[-1].date().isoformat(),
        "price": round(price, 2),
        "return_1y_pct": trailing_return(252),
        "return_3y_pct": trailing_return(756),
        "max_drawdown_2y_pct": round(dd, 1),
        "realized_vol_1y_pct": round(vol, 1) if vol else None,
        "rsi_14": round(rsi, 1),
        "pct_vs_sma200": round((price / sma200 - 1) * 100, 1) if sma200 else None,
    }


def _post_date_split_factor(ticker: str, as_of: date) -> float:
    """Cumulative split factor for splits AFTER as_of.

    yfinance prices are split-adjusted to today, but SEC shares/EPS are
    as-reported in the then-current share count. To reconcile, scale SEC
    shares UP and SEC EPS DOWN by the product of all split ratios that
    occurred after as_of (e.g. NVDA: 4:1 in 2021 × 10:1 in 2024 = 40x).
    """
    try:
        splits = _history(ticker)  # ensure ticker object built
        sp = yf.Ticker(ticker).splits
    except Exception:
        return 1.0
    factor = 1.0
    import pandas as pd
    cut = pd.Timestamp(as_of, tz=sp.index.tz) if len(sp) else None
    for dt, ratio in sp.items():
        if cut is not None and dt > cut and ratio:
            factor *= float(ratio)
    return factor or 1.0


def snapshot(ticker: str, as_of: date) -> dict:
    """Full point-in-time snapshot: fundamentals + valuation + technicals + Z."""
    fund = fundamentals_as_of(ticker, as_of)
    px = price_metrics_as_of(ticker, as_of)
    price = px.get("price")  # split-adjusted to today
    split_factor = _post_date_split_factor(ticker, as_of)
    shares = fund.get("diluted_shares")
    shares_adj = shares * split_factor if shares else None
    mcap = price * shares_adj if (price and shares_adj) else None

    eps = fund.get("eps_diluted")
    eps = (eps / split_factor) if (eps is not None and split_factor) else eps
    rev = fund.get("revenue")
    equity = fund.get("equity")
    valuation = {
        "market_cap": mcap,
        "pe": round(price / eps, 1) if (price and eps and eps > 0) else None,
        "ps": round(mcap / rev, 2) if (mcap and rev) else None,
        "pb": round(mcap / equity, 2) if (mcap and equity and equity > 0) else None,
        "split_factor_applied": split_factor,
    }
    z = altman_z(fund, mcap)
    return {"ticker": ticker, "as_of": as_of.isoformat(),
            "fundamentals": fund, "valuation": valuation,
            "altman_z": z, "price_metrics": px}


def forward_return(ticker: str, as_of: date, days: int) -> float | None:
    """LOOK-AHEAD: total return over `days` after as_of. For scoring only."""
    import pandas as pd
    hist = _history(ticker)
    adj = hist["Adj Close"].dropna()
    cut = pd.Timestamp(as_of, tz=hist.index.tz)
    before = adj[adj.index <= cut]
    after = adj[adj.index >= cut + pd.Timedelta(days=days)]
    if before.empty or after.empty:
        return None
    return round((after.iloc[0] / before.iloc[-1] - 1) * 100, 1)


def annual_history_as_of(ticker: str, as_of: date, n: int = 5) -> list[dict]:
    """Last `n` annual revenue/operating-income/EPS values KNOWN as of `as_of`.

    Enables normalized-earnings and cyclical-vs-structural classification point
    in time (no look-ahead): only periods filed on/before as_of are included.
    """
    gaap = companyfacts(ticker)["facts"]["us-gaap"]

    def series(names, kind):
        cand = []
        for f in _facts_for(gaap, names):
            filed, end = f.get("filed"), f.get("end")
            if not filed or not end:
                continue
            if datetime.strptime(filed, "%Y-%m-%d").date() > as_of:
                continue
            if kind == "duration":
                start = f.get("start")
                if not start:
                    continue
                span = (datetime.strptime(end, "%Y-%m-%d").date()
                        - datetime.strptime(start, "%Y-%m-%d").date()).days
                if not (330 <= span <= 400):
                    continue
            cand.append(f)
        byend = {}
        for f in cand:  # dedup by period end, keep latest-filed
            e = f["end"]
            if e not in byend or f["filed"] > byend[e]["filed"]:
                byend[e] = f
        rows = sorted(byend.values(), key=lambda x: x["end"])[-n:]
        return {r["end"]: r["val"] for r in rows}

    rev, op, eps = series(REVENUE, "duration"), series(OP_INCOME, "duration"), series(EPS_DIL, "duration")
    ends = sorted(set(rev) | set(op) | set(eps))
    return [{"end": e, "revenue": rev.get(e), "op_income": op.get(e), "eps": eps.get(e)}
            for e in ends]


def altman_z(fund: dict, market_cap: float | None) -> dict:
    """Altman Z-score (classic public-manufacturer form). <1.81 distress."""
    ta = fund.get("total_assets")
    tl = fund.get("total_liabilities")
    wc = fund["_raw"].get("working_capital")
    re = fund.get("retained_earnings")
    ebit = fund["_raw"].get("ebit")
    sales = fund.get("revenue")
    if not all(v is not None for v in [ta, tl, wc, re, ebit, sales]) or not ta or market_cap is None:
        return {"z": None, "zone": None}
    z = (1.2 * wc / ta + 1.4 * re / ta + 3.3 * ebit / ta
         + 0.6 * market_cap / tl + 1.0 * sales / ta)
    zone = ("distress" if z < 1.81 else "grey" if z < 2.99 else "safe")
    return {"z": round(z, 2), "zone": zone}
