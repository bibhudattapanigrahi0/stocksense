"""
Paper-trading account — a virtual book to trial ideas before risking real capital.

One account per market: a starting balance, open positions, and a closed-trade history. A position
opens at a price with an optional stop/target and a **source** label (which strategy / bot / idea
suggested it), marks to live quotes, and closes on a stop/target touch or a manual close — booking
realized P&L. The account reports equity, realized/unrealized P&L, a win record, and **P&L
attribution by source**, so you can see which strategies actually make money on your book.

The account math is pure (prices are injected) and lives in the top half of this module; the async
wrappers that load/persist the account (`run_store`) and fetch live quotes (`market_data`) are in
the bottom half, cleanly separated so the core stays unit-testable.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from pipeline_core.services import market_data, run_store
from pipeline_core.utils import round_price

DEFAULT_START = 100_000.0
DEFAULT_ALLOC_PCT = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_account(market: str, start_balance: float = DEFAULT_START) -> dict:
    return {"market": (market or "IN").upper(), "start_balance": float(start_balance),
            "positions": [], "closed": [], "created_at": _now(), "updated_at": _now()}


def _pnl(side: str, entry: float, price: float, qty: float) -> float:
    return qty * ((price - entry) if side == "long" else (entry - price))


def _realized(acct: dict) -> float:
    return sum(c.get("pnl", 0.0) for c in acct["closed"])


def book_equity(acct: dict) -> float:
    """Equity excluding open positions' unrealized P&L (start + realized) — the sizing base."""
    return acct["start_balance"] + _realized(acct)


def open_position(acct: dict, ticker: str, side: str, price: float, *, qty: Optional[float] = None,
                  alloc_pct: Optional[float] = None, stop=None, target=None, source: str = "manual") -> dict:
    """Open a paper position at `price`, sized by explicit `qty` or by `alloc_pct` of book equity
    (default 10%). Raises ValueError on bad input."""
    price = float(price)
    side = (side or "long").lower()
    if price <= 0:
        raise ValueError("price must be positive")
    if side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    if qty is None:
        ap = float(alloc_pct if alloc_pct is not None else DEFAULT_ALLOC_PCT)
        qty = (book_equity(acct) * ap / 100.0) / price
    qty = round(float(qty), 6)
    if qty <= 0:
        raise ValueError("qty must be positive")
    pos = {
        "id": uuid.uuid4().hex[:12], "ticker": ticker.upper(), "side": side, "qty": qty,
        "entry_price": round_price(price), "stop": round_price(stop) if stop else None,
        "target": round_price(target) if target else None, "source": source or "manual",
        "opened_at": _now(), "notional": round_price(qty * price),
    }
    acct["positions"].append(pos)
    acct["updated_at"] = _now()
    return pos


def close_position(acct: dict, position_id: str, price: float, *, reason: str = "manual") -> Optional[dict]:
    idx = next((i for i, p in enumerate(acct["positions"]) if p["id"] == position_id), None)
    if idx is None:
        return None
    pos = acct["positions"].pop(idx)
    price = float(price)
    pnl = _pnl(pos["side"], pos["entry_price"], price, pos["qty"])
    ret = (price / pos["entry_price"] - 1.0) * 100.0 * (1 if pos["side"] == "long" else -1)
    closed = {**pos, "exit_price": round_price(price), "closed_at": _now(), "reason": reason,
              "pnl": round(pnl, 2), "return_pct": round(ret, 2)}
    acct["closed"].append(closed)
    acct["updated_at"] = _now()
    return closed


def _auto_close_hits(acct: dict, prices: dict) -> list[dict]:
    """Close any open position whose stop/target the marked price has touched. Returns the closures."""
    done = []
    for pos in list(acct["positions"]):
        px = prices.get(pos["ticker"])
        if px is None:
            continue
        hit = None
        if pos["side"] == "long":
            if pos["stop"] and px <= pos["stop"]:
                hit = ("stop", pos["stop"])
            elif pos["target"] and px >= pos["target"]:
                hit = ("target", pos["target"])
        else:
            if pos["stop"] and px >= pos["stop"]:
                hit = ("stop", pos["stop"])
            elif pos["target"] and px <= pos["target"]:
                hit = ("target", pos["target"])
        if hit:
            done.append(close_position(acct, pos["id"], hit[1], reason=hit[0]))
    return done


def summary(acct: dict, prices: dict) -> dict:
    """Mark the account to `prices` (ticker→price) and return a full summary (no mutation)."""
    open_rows, unreal, deployed = [], 0.0, 0.0
    for p in acct["positions"]:
        px = prices.get(p["ticker"])
        pnl = _pnl(p["side"], p["entry_price"], px, p["qty"]) if px is not None else None
        if pnl is not None:
            unreal += pnl
        deployed += p["qty"] * p["entry_price"]
        open_rows.append({
            **p, "price": round_price(px) if px is not None else None,
            "unrealized_pnl": round(pnl, 2) if pnl is not None else None,
            "return_pct": round(_pnl(p["side"], p["entry_price"], px, 1) / p["entry_price"] * 100, 2)
            if px is not None else None,
        })
    realized = _realized(acct)
    equity = acct["start_balance"] + realized + unreal
    closed = acct["closed"]
    wins = sum(1 for c in closed if c["pnl"] > 0)

    attr: dict[str, dict] = {}
    for c in closed:
        a = attr.setdefault(c.get("source", "manual"), {"pnl": 0.0, "trades": 0, "wins": 0})
        a["pnl"] = round(a["pnl"] + c["pnl"], 2)
        a["trades"] += 1
        a["wins"] += 1 if c["pnl"] > 0 else 0

    return {
        "market": acct["market"], "start_balance": acct["start_balance"],
        "equity": round(equity, 2), "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unreal, 2),
        "return_pct": round((equity / acct["start_balance"] - 1) * 100, 2) if acct["start_balance"] else 0.0,
        "deployed_notional": round(deployed, 2),
        "open_positions": open_rows, "open_count": len(open_rows),
        "closed_count": len(closed), "wins": wins, "losses": len(closed) - wins,
        "win_rate": round(wins / len(closed) * 100, 1) if closed else None,
        "attribution": [
            {"source": k, **v, "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else None}
            for k, v in sorted(attr.items(), key=lambda kv: -kv[1]["pnl"])
        ],
        "closed": closed[-50:],
        "updated_at": acct["updated_at"],
    }


# ── async wrappers (persistence + live pricing) ─────────────────────


async def _load_or_create(market: str, user_id: Optional[str] = None) -> dict:
    acct = await run_store.get_paper(market, user_id=user_id)
    return acct if acct else new_account(market)


async def _quotes(market: str, tickers: list[str]) -> dict:
    if not tickers:
        return {}
    sem = asyncio.Semaphore(8)

    async def one(tk):
        async with sem:
            try:
                return tk, await asyncio.to_thread(market_data.get_quote, tk, market)
            except Exception:
                return tk, None

    return {tk: px for tk, px in await asyncio.gather(*(one(t) for t in tickers)) if px is not None}


async def mark_account(market: str, user_id: Optional[str] = None) -> dict:
    """Load the account, mark to live quotes, auto-close stop/target hits, persist if anything
    changed, and return the summary."""
    acct = await _load_or_create(market, user_id)
    prices = await _quotes(market, [p["ticker"] for p in acct["positions"]])
    closed = _auto_close_hits(acct, prices)
    if closed:
        await run_store.save_paper(acct, user_id=user_id)
    return summary(acct, prices)


async def open_idea(market: str, ticker: str, side: str, *, qty=None, alloc_pct=None,
                    stop=None, target=None, price=None, source: str = "manual",
                    user_id: Optional[str] = None) -> dict:
    """Open a paper position, pricing at `price` or a live quote, then persist. Returns the summary."""
    acct = await _load_or_create(market, user_id)
    if price is None:
        price = await asyncio.to_thread(market_data.get_quote, ticker, market)
    if price is None:
        return {"error": f"no live price for {ticker}"}
    open_position(acct, ticker, side, price, qty=qty, alloc_pct=alloc_pct,
                  stop=stop, target=target, source=source)
    await run_store.save_paper(acct, user_id=user_id)
    prices = await _quotes(market, [p["ticker"] for p in acct["positions"]])
    return summary(acct, prices)


async def close_idea(market: str, position_id: str, *, price=None, user_id: Optional[str] = None) -> dict:
    """Close one paper position at `price` or a live quote, then persist. Returns the summary."""
    acct = await _load_or_create(market, user_id)
    pos = next((p for p in acct["positions"] if p["id"] == position_id), None)
    if pos is None:
        return {"error": "position not found"}
    if price is None:
        price = await asyncio.to_thread(market_data.get_quote, pos["ticker"], market)
    if price is None:
        return {"error": f"no live price for {pos['ticker']}"}
    close_position(acct, position_id, price, reason="manual")
    await run_store.save_paper(acct, user_id=user_id)
    prices = await _quotes(market, [p["ticker"] for p in acct["positions"]])
    return summary(acct, prices)


async def reset_account(market: str, start_balance: float = DEFAULT_START, user_id: Optional[str] = None) -> dict:
    acct = new_account(market, start_balance)
    await run_store.save_paper(acct, user_id=user_id)
    return summary(acct, {})
