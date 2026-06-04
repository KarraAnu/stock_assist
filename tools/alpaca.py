"""Alpaca PAPER-trading connector.

SAFETY: the base URL is hardcoded to Alpaca's paper endpoint. These tools can
never touch a live brokerage account or real money — there is no live-trading
code path here by construction.

Purpose: this is the execution + tracking layer for the learning autopilot. The
point is not profit; it's to forward-test the screen's decisions on a simulated
account and log outcomes so the rules and tools can be improved later.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

# Hardcoded paper endpoint — NOT configurable, so live trading is impossible here.
_BASE = "https://paper-api.alpaca.markets"


class AlpacaError(Exception):
    pass


def _keys() -> tuple[str | None, str | None]:
    return os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")


def have_keys() -> bool:
    k, s = _keys()
    return bool(k and s)


def _headers() -> dict:
    k, s = _keys()
    return {"APCA-API-KEY-ID": k or "", "APCA-API-SECRET-KEY": s or ""}


def _request(method: str, path: str, **kwargs):
    if not have_keys():
        raise AlpacaError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set — add your "
                          "Alpaca *paper* keys to the MCP client config env block.")
    resp = requests.request(method, f"{_BASE}{path}", headers=_headers(),
                            timeout=20, **kwargs)
    if resp.status_code >= 400:
        raise AlpacaError(f"Alpaca {path} HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.text else {}


def paper_account() -> dict:
    """Paper-trading account snapshot: equity, cash, buying power, P&L.

    Returns the simulated account's current equity, cash, buying power, and
    today's change. Confirms the Alpaca paper connection is live.
    """
    try:
        a = _request("GET", "/v2/account")
    except AlpacaError as exc:
        return {"error": str(exc)}
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    equity = f(a.get("equity"))
    last_equity = f(a.get("last_equity"))
    return {
        "account_number": a.get("account_number"),
        "status": a.get("status"),
        "currency": a.get("currency"),
        "equity": equity,
        "cash": f(a.get("cash")),
        "buying_power": f(a.get("buying_power")),
        "portfolio_value": f(a.get("portfolio_value")),
        "today_change": round(equity - last_equity, 2)
        if (equity is not None and last_equity is not None) else None,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "mode": "PAPER (simulated — no real money)",
    }


def paper_positions() -> dict:
    """Current paper-trading positions with unrealized P&L.

    Returns each held position: symbol, quantity, average entry, current price,
    market value, and unrealized gain/loss (absolute and %).
    """
    try:
        positions = _request("GET", "/v2/positions")
    except AlpacaError as exc:
        return {"error": str(exc)}
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    out = []
    for p in positions:
        out.append({
            "symbol": p.get("symbol"),
            "qty": f(p.get("qty")),
            "avg_entry_price": f(p.get("avg_entry_price")),
            "current_price": f(p.get("current_price")),
            "market_value": f(p.get("market_value")),
            "unrealized_pl": f(p.get("unrealized_pl")),
            "unrealized_pl_pct": round(f(p.get("unrealized_plpc")) * 100, 2)
            if f(p.get("unrealized_plpc")) is not None else None,
        })
    return {"count": len(out), "positions": out, "mode": "PAPER"}


def paper_place_order(symbol: str, qty: float, side: str = "buy") -> dict:
    """Place a simulated market order on the paper account.

    `side` is "buy" or "sell"; `qty` is the number of shares (fractional allowed).
    Executes against Alpaca's PAPER account only — no real money. Returns the
    created order. Use paper_account / paper_positions to see the effect.
    """
    side = (side or "").strip().lower()
    if side not in {"buy", "sell"}:
        return {"error": "side must be 'buy' or 'sell'"}
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return {"error": "qty must be a number"}
    if qty <= 0:
        return {"error": "qty must be positive"}

    body = {"symbol": symbol.upper(), "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day"}
    try:
        o = _request("POST", "/v2/orders", json=body)
    except AlpacaError as exc:
        return {"error": str(exc)}
    return {
        "order_id": o.get("id"),
        "symbol": o.get("symbol"),
        "qty": o.get("qty"),
        "side": o.get("side"),
        "type": o.get("type"),
        "status": o.get("status"),
        "submitted_at": o.get("submitted_at"),
        "mode": "PAPER (simulated)",
    }


def paper_orders(limit: int = 20) -> dict:
    """Recent paper-trading orders (filled and pending), newest first."""
    try:
        orders = _request("GET", "/v2/orders",
                          params={"status": "all", "limit": max(1, min(int(limit), 100))})
    except AlpacaError as exc:
        return {"error": str(exc)}
    out = []
    for o in orders:
        out.append({
            "symbol": o.get("symbol"), "qty": o.get("qty"), "side": o.get("side"),
            "status": o.get("status"), "filled_avg_price": o.get("filled_avg_price"),
            "submitted_at": o.get("submitted_at"),
        })
    return {"count": len(out), "orders": out, "mode": "PAPER"}
