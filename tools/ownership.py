"""Ownership signals: insider transactions, institutional %, short interest.

Dogfooding gap: judging a name's smart-money positioning required WebFetching
MarketBeat mid-analysis. Insider trades come from FMP (paid endpoint);
institutional/insider percentages and short interest come from yfinance, so
the tool still returns the yfinance half if FMP is unavailable.
"""

from __future__ import annotations

from datetime import date, timedelta

from .common import get_info, normalize_ticker, num
from .fmp import FMPError, fmp_get, have_key

_INSIDER_WINDOW_DAYS = 180


def _pct(x) -> float | None:
    v = num(x)
    return round(v * 100, 2) if v is not None else None


def get_ownership(ticker: str) -> dict:
    """Who owns the stock and which way insiders are leaning.

    Returns short interest (% of float, days-to-cover, month-over-month
    change), institutional and insider ownership percentages, and a summary of
    open-market insider transactions over the last 6 months (buys vs sells by
    count and dollar value, plus the largest recent transactions). Insider
    BUYS are the informative side — insiders sell for many reasons but buy for
    one. Stock-grant awards/gifts are excluded as noise.

    Short interest and ownership percentages come from yfinance; insider
    transactions need FMP. Fails soft per section.
    """
    ticker = normalize_ticker(ticker)
    if not ticker:
        return {"error": "no ticker provided"}

    info = get_info(ticker)
    flags: list[str] = []

    short_pct = _pct(info.get("shortPercentOfFloat"))
    shares_short = num(info.get("sharesShort"))
    shares_short_prior = num(info.get("sharesShortPriorMonth"))
    short = {
        "short_pct_of_float": short_pct,
        "days_to_cover": num(info.get("shortRatio")),
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
    }
    if shares_short and shares_short_prior and shares_short_prior > 0:
        chg = (shares_short / shares_short_prior - 1) * 100
        short["mom_change_pct"] = round(chg, 1)
        if abs(chg) >= 15:
            flags.append(f"short interest {'rose' if chg > 0 else 'fell'} "
                         f"{abs(chg):.0f}% month-over-month")
    if short_pct is not None and short_pct >= 10:
        flags.append(f"high short interest ({short_pct:.0f}% of float) — "
                     f"crowded bearish positioning")

    holders = {
        "institutional_pct": _pct(info.get("heldPercentInstitutions")),
        "insider_pct": _pct(info.get("heldPercentInsiders")),
    }

    # --- insider transactions (FMP) ---
    insider: dict = {"available": False}
    if have_key():
        try:
            rows = fmp_get("insider-trading/search", symbol=ticker,
                           page=0, limit=100)
            cutoff = (date.today() - timedelta(days=_INSIDER_WINDOW_DAYS)).isoformat()
            buys, sells, transactions = [], [], []
            strat_buy, strat_sell, strat_count = 0.0, 0.0, 0
            for r in rows if isinstance(rows, list) else []:
                tdate = str(r.get("transactionDate") or "")
                ttype = str(r.get("transactionType") or "")
                if tdate < cutoff:
                    continue
                # Open-market purchases/sales only — awards, gifts, and tax
                # withholding are administrative noise.
                side = ("buy" if ttype.startswith("P-") else
                        "sell" if ttype.startswith("S-") else None)
                if side is None:
                    continue
                shares = num(r.get("securitiesTransacted")) or 0
                price = num(r.get("price")) or 0
                value = shares * price
                # 10%-owners are funds/strategic holders distributing — a
                # different signal from management conviction (dogfooding: a
                # fund unwind read as "$5.3B of insider selling" on NRG).
                owner = str(r.get("typeOfOwner") or "").lower()
                strategic = "10 percent owner" in owner or "10% owner" in owner
                if strategic:
                    strat_count += 1
                    if side == "buy":
                        strat_buy += value
                    else:
                        strat_sell += value
                    continue
                (buys if side == "buy" else sells).append(value)
                transactions.append({
                    "date": tdate, "side": side,
                    "name": r.get("reportingName"),
                    "title": r.get("typeOfOwner"),
                    "shares": shares, "price": price,
                    "value": round(value),
                })
            transactions.sort(key=lambda t: t["value"], reverse=True)
            insider = {
                "available": True,
                "window_days": _INSIDER_WINDOW_DAYS,
                "scope": "officers & directors (10%-owner trades reported "
                         "separately — fund distributions are not management "
                         "conviction)",
                "buy_count": len(buys),
                "sell_count": len(sells),
                "buy_value": round(sum(buys)),
                "sell_value": round(sum(sells)),
                "net_value": round(sum(buys) - sum(sells)),
                "largest_transactions": transactions[:5],
            }
            if strat_count:
                insider["strategic_holders_10pct"] = {
                    "transaction_count": strat_count,
                    "net_value": round(strat_buy - strat_sell),
                }
            # Flag the NET position — "insiders bought $115K" is misleading
            # when they also sold $1.9M.
            if buys or sells:
                total_b, total_s = sum(buys), sum(sells)
                net = total_b - total_s
                months = _INSIDER_WINDOW_DAYS // 30
                if net > 0:
                    flags.append(f"insider NET BUYING ${net:,.0f} over {months} "
                                 f"months ({len(buys)} buy(s) ${total_b:,.0f} vs "
                                 f"{len(sells)} sell(s) ${total_s:,.0f})")
                elif net < 0:
                    flags.append(f"insider net selling ${abs(net):,.0f} over "
                                 f"{months} months ({len(sells)} sell(s) "
                                 f"${total_s:,.0f} vs {len(buys)} buy(s) "
                                 f"${total_b:,.0f})")
        except FMPError as exc:
            insider = {"available": False,
                       "reason": f"FMP insider endpoint unavailable: {str(exc)[:120]}"}
    else:
        insider = {"available": False, "reason": "FMP_API_KEY not set"}

    return {
        "ticker": ticker,
        "short_interest": short,
        "holders": holders,
        "insider_activity": insider,
        "flags": flags,
        "source": "yfinance (short interest, holder %) + FMP (insider transactions)",
    }
