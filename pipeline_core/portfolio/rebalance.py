"""
Portfolio rebalancing / drift.

Given a saved `Portfolio`, reconstruct the fresh target from its stored `spec` and
diff it against the currently-held positions:

  BUY   — in the fresh target, not currently held
  EXIT  — currently held, no longer selected by the strategy/theme/risk
  ADD   — held, but target weight is meaningfully higher (drift beyond the band)
  TRIM  — held, but target weight is meaningfully lower
  HOLD  — held and within the drift band

Live prices (optional) mark each held name's return vs its recorded entry. The caller
loads the portfolio and passes it in, so this module never imports the persistence
layer (keeps it free of import cycles).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from pipeline_core.portfolio.construct import construct_portfolio
from pipeline_core.portfolio.models import Portfolio, RebalanceAction, RebalanceResult

# Drift band: weight moves smaller than this are HOLD, not ADD/TRIM.
DRIFT_BAND = 0.02

PriceFn = Callable[[str], Awaitable[Optional[float]]]


async def rebalance_portfolio(
    portfolio: Portfolio,
    *,
    screen_fn=None,
    info_fn=None,
    price_fn: Optional[PriceFn] = None,
    mark_prices: bool = True,
) -> RebalanceResult:
    """Compute the buy/trim/exit actions to move `portfolio` to its fresh target.

    `mark_prices` (default True) fetches the live price of each held name to compute an
    unrealized return; pass a fake `price_fn` (or `mark_prices=False`) to stay offline.
    """
    target = await construct_portfolio(portfolio.spec, screen_fn=screen_fn, info_fn=info_fn)

    held = {p.ticker: p for p in portfolio.positions}
    tgt = {p.ticker: p for p in target.positions}

    # Optional live marks for held names.
    prices: dict[str, Optional[float]] = {}
    if mark_prices and held:
        if price_fn is None:
            from pipeline_core.backtest import runner

            async def price_fn(tk: str) -> Optional[float]:  # type: ignore[misc]
                return await asyncio.to_thread(runner._current_price, tk)

        sem = asyncio.Semaphore(8)

        async def one(tk: str):
            async with sem:
                try:
                    return tk, await price_fn(tk)
                except Exception:
                    return tk, None

        prices = dict(await asyncio.gather(*(one(tk) for tk in held)))

    actions: list[RebalanceAction] = []
    for tk in sorted(set(held) | set(tgt)):
        h = held.get(tk)
        t = tgt.get(tk)
        hw = round(h.weight, 4) if h else 0.0
        tw = round(t.weight, 4) if t else 0.0
        drift = round((tw - hw) * 100, 2)

        if h and t:
            if tw > hw + DRIFT_BAND:
                action, reason = "ADD", "target weight above holding"
            elif tw < hw - DRIFT_BAND:
                action, reason = "TRIM", "target weight below holding"
            else:
                action, reason = "HOLD", "within drift band"
        elif t and not h:
            action, reason = "BUY", "newly selected by the strategy/theme/risk"
        else:
            action, reason = "EXIT", "no longer selected"

        entry = h.entry_price if h else None
        cur = prices.get(tk) if h else None
        ret = round((cur - entry) / entry * 100, 2) if (cur is not None and entry) else None

        actions.append(RebalanceAction(
            ticker=tk, action=action, held_weight=hw, target_weight=tw, drift_pct=drift,
            current_price=cur, entry_price=entry, return_pct=ret, reason=reason,
        ))

    order = {"EXIT": 0, "BUY": 1, "TRIM": 2, "ADD": 3, "HOLD": 4}
    actions.sort(key=lambda a: (order.get(a.action, 9), -abs(a.drift_pct)))

    counts: dict[str, int] = {}
    for a in actions:
        counts[a.action] = counts.get(a.action, 0) + 1

    return RebalanceResult(
        portfolio_id=portfolio.id,
        name=portfolio.name,
        actions=actions,
        summary={
            "counts": counts,
            "target_positions": len(tgt),
            "held_positions": len(held),
            "target_notes": target.summary.get("notes", []),
        },
        as_of=datetime.now(timezone.utc).isoformat(),
        disclaimer=target.disclaimer,
    )
