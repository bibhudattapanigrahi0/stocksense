"""
Portfolio mark-to-market tracking.

A saved `Portfolio` is a static snapshot (entry prices frozen at construction).
`snapshot_portfolio` marks every position to its live price — market-aware via
`portfolio.spec.market` — computes each name's return vs its entry and a
weight-weighted portfolio return (cash counts as flat), and records the point as a
NAV time-series entry via run_store. Plotted over time, this is the portfolio's
realized NAV / return curve.

Reuses `runner._current_price` (the same live-price fetch the idea tracker and
rebalance use); a fake `price_fn` keeps it offline for tests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from pipeline_core.portfolio.models import Portfolio
from pipeline_core.utils import round_price

PriceFn = Callable[[str], Awaitable[Optional[float]]]


async def snapshot_portfolio(
    portfolio: Portfolio,
    *,
    price_fn: Optional[PriceFn] = None,
    persist: bool = True,
) -> dict:
    """Mark the portfolio to live prices and (optionally) record a NAV snapshot.

    Returns the snapshot dict: `ts`, `nav` (1.0 = cost basis), `return_pct`,
    per-position `positions`, and how many names were priced.
    """
    market = portfolio.spec.market if portfolio.spec else "IN"
    positions = portfolio.positions or []

    if price_fn is None:
        from pipeline_core.backtest import runner

        async def price_fn(tk: str) -> Optional[float]:  # type: ignore[misc]
            return await asyncio.to_thread(runner._current_price, tk, market)

    sem = asyncio.Semaphore(8)

    async def one(tk: str):
        async with sem:
            try:
                return tk, await price_fn(tk)
            except Exception:
                return tk, None

    prices = dict(await asyncio.gather(*(one(p.ticker) for p in positions))) if positions else {}

    marks: list[dict] = []
    port_ret = 0.0          # whole-portfolio return (cash + unpriced count as flat)
    priced = 0
    for p in positions:
        cur = prices.get(p.ticker)
        entry = p.entry_price
        ret = (cur - entry) / entry if (cur is not None and entry) else None
        if ret is not None:
            port_ret += p.weight * ret
            priced += 1
        marks.append({
            "ticker": p.ticker,
            "weight": round(p.weight, 4),
            "entry_price": round_price(entry),
            "current_price": round_price(cur),
            "return_pct": round(ret * 100, 2) if ret is not None else None,
        })

    invested = round(sum(p.weight for p in positions), 4)
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "nav": round(1.0 + port_ret, 4),
        "return_pct": round(port_ret * 100, 2),
        "invested_pct": invested,
        "priced": priced,
        "positions_total": len(positions),
        "positions": marks,
    }

    if persist and portfolio.id:
        from pipeline_core.services import run_store

        await run_store.save_portfolio_snapshot(portfolio.id, snapshot)

    return snapshot
