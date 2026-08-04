"""
Crypto Momentum Bot — a BTC-regime-gated, volatility-targeted momentum basket.

The edge: alts are levered Bitcoin — they trend hard when BTC trends and bleed when
it doesn't, and crypto's fat tails punish equal-weighting. So the bot:

  1. **Regime gate** — only holds risk assets while BTC is above its 200-day MA;
     otherwise it goes to cash (a flat, "risk-off" signal).
  2. **Momentum rank** — ranks the tradable top-N universe (CoinGecko market-cap list,
     Binance OHLCV) by a blend of 30- and 90-day return and takes the strongest.
  3. **Inverse-vol sizing** — weights ∝ 1/volatility (capped per name), so a 120%-vol
     coin gets a smaller slice than a 60%-vol one.

`generate_signal` returns today's target basket (what to hold and why). `deploy`
materializes it as a saved Portfolio so its NAV accrues over time via the
portfolio-snapshot machinery — cost basis is carried over for names the bot keeps,
so the NAV curve stays meaningful across rebalances.

Reuses: markets.get_universe_tickers, market_data price history (Binance for crypto),
agents.indicators.sma, utils.round_price, portfolio.track.snapshot_portfolio.
Long-only and self-contained; a scheduled run (cron/routine) makes it a live bot.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from pipeline_core.agents import indicators
from pipeline_core.config import settings
from pipeline_core.markets import get_universe_tickers
from pipeline_core.services import market_data
from pipeline_core.utils import round_price

logger = logging.getLogger(__name__)

BOT_ID = "crypto-momentum"
MARKET = "CRYPTO"
BENCHMARK = "BTC"                    # regime + benchmark

DEFAULTS = {
    "top_n": 10,
    "universe": "Top 50",
    "stop_pct": 0.15,               # wide trailing stop — crypto gives moves back fast
    "max_weight": 0.25,             # per-name cap so one coin can't dominate
    "portfolio_name": "Crypto Momentum Bot",
}


# ── Metrics (pure) ──────────────────────────────────────────────────


def _trailing_return(close: pd.Series, days: int) -> Optional[float]:
    if len(close) <= days:
        return None
    past = float(close.iloc[-days - 1])
    return (float(close.iloc[-1]) - past) / past if past > 0 else None


def _annual_vol(close: pd.Series) -> Optional[float]:
    """Annualized volatility from daily returns (crypto trades 365 days)."""
    rets = close.pct_change().dropna()
    if len(rets) < 20:
        return None
    sd = float(rets.std())
    return sd * math.sqrt(365) if sd > 0 else None


def _momentum(close: pd.Series) -> Optional[float]:
    """Blend of 30- and 90-day return (relative-strength score), as a fraction."""
    r30, r90 = _trailing_return(close, 30), _trailing_return(close, 90)
    if r30 is None and r90 is None:
        return None
    parts = [(0.4, r30), (0.6, r90)]
    num = sum(w * r for w, r in parts if r is not None)
    den = sum(w for w, r in parts if r is not None)
    return num / den if den else None


def _btc_regime(df: pd.DataFrame) -> tuple[bool, str]:
    """Risk-on when BTC closes above its 200-day MA (100-DMA fallback on short history)."""
    if df is None or df.empty or "Close" not in df:
        return False, "no BTC price data"
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])
    for length in (200, 100, 50):
        ma = indicators.sma(close, length)
        val = ma.iloc[-1]
        if val == val:  # not NaN
            on = last > float(val)
            return on, f"BTC {round_price(last)} {'>' if on else '≤'} {length}-DMA {round_price(float(val))}"
    return False, "insufficient BTC history"


def _inverse_vol_weights(vols: list[float], max_weight: float) -> list[float]:
    """Weights ∝ 1/vol, capped at max_weight and renormalized to sum to 1."""
    inv = [1.0 / max(v, 0.01) for v in vols]
    total = sum(inv) or 1.0
    w = [x / total for x in inv]
    for _ in range(6):  # iteratively cap + spill the excess onto uncapped names
        over = [i for i, x in enumerate(w) if x > max_weight + 1e-9]
        if not over:
            break
        excess = sum(w[i] - max_weight for i in over)
        for i in over:
            w[i] = max_weight
        under = [i for i in range(len(w)) if i not in over]
        under_tot = sum(w[i] for i in under)
        if under_tot <= 0:
            break
        for i in under:
            w[i] += excess * (w[i] / under_tot)
    return w


# ── Signal ──────────────────────────────────────────────────────────


async def _load_histories(symbols: list[str]) -> dict[str, pd.DataFrame]:
    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def one(sym: str):
        async with sem:
            return sym, await market_data.aget_price_history(sym, period="1y", interval="1d", market=MARKET)

    return dict(await asyncio.gather(*(one(s) for s in symbols)))


async def generate_signal(
    top_n: int = DEFAULTS["top_n"],
    universe: str = DEFAULTS["universe"],
    stop_pct: float = DEFAULTS["stop_pct"],
    max_weight: float = DEFAULTS["max_weight"],
) -> dict:
    """Compute today's target basket. Risk-off (BTC below its trend) → all cash."""
    top_n = max(1, min(int(top_n), 25))
    as_of = datetime.now(timezone.utc).isoformat()

    btc_df = await market_data.aget_price_history(BENCHMARK, period="1y", interval="1d", market=MARKET)
    regime_on, regime_detail = _btc_regime(btc_df)

    base = {
        "bot": BOT_ID, "market": MARKET, "as_of": as_of, "universe": universe,
        "regime_detail": regime_detail, "params": {"top_n": top_n, "stop_pct": stop_pct, "max_weight": max_weight},
        "disclaimer": "Decision-support research, not investment advice. Crypto is volatile and price-only.",
    }
    if not regime_on:
        return {**base, "regime": "risk-off", "cash_pct": 1.0, "basket": [],
                "note": "BTC is below its trend — the bot holds cash and takes no new positions."}

    symbols = await asyncio.to_thread(get_universe_tickers, MARKET, universe)
    histories = await _load_histories(symbols)

    scored = []
    for sym, df in histories.items():
        if df is None or df.empty or len(df) < 60:
            continue
        close = df["Close"].astype(float)
        mom, vol = _momentum(close), _annual_vol(close)
        if mom is None or vol is None:
            continue
        scored.append({
            "ticker": sym,
            "momentum": mom,
            "return_30d_pct": round((_trailing_return(close, 30) or 0) * 100, 1),
            "return_90d_pct": round((_trailing_return(close, 90) or 0) * 100, 1),
            "volatility_pct": round(vol * 100, 1),
            "price": round_price(float(close.iloc[-1])),
        })

    scored.sort(key=lambda r: r["momentum"], reverse=True)
    top = [r for r in scored if r["momentum"] > 0][:top_n]   # only positive momentum
    if not top:
        return {**base, "regime": "risk-on", "cash_pct": 1.0, "basket": [],
                "note": "Regime is risk-on but nothing has positive momentum — the bot stays in cash."}

    weights = _inverse_vol_weights([r["volatility_pct"] / 100 for r in top], max_weight)
    basket = []
    for r, w in zip(top, weights):
        basket.append({
            "ticker": r["ticker"],
            "weight": round(w, 4),
            "price": r["price"],
            "stop_price": round_price(r["price"] * (1 - stop_pct)) if r["price"] else None,
            "momentum_score": round(r["momentum"] * 100, 1),
            "return_30d_pct": r["return_30d_pct"],
            "return_90d_pct": r["return_90d_pct"],
            "volatility_pct": r["volatility_pct"],
        })

    return {**base, "regime": "risk-on", "cash_pct": 0.0, "basket": basket,
            "note": f"Holding the top {len(basket)} momentum coins, inverse-vol weighted "
                    f"(capped {int(max_weight * 100)}%/name), {int(stop_pct * 100)}% trailing stop."}


# ── Deploy as a tracked portfolio (NAV over time) ───────────────────


async def deploy(
    top_n: int = DEFAULTS["top_n"],
    universe: str = DEFAULTS["universe"],
    stop_pct: float = DEFAULTS["stop_pct"],
    max_weight: float = DEFAULTS["max_weight"],
    name: str = DEFAULTS["portfolio_name"],
    ai: bool = False,
    apply_ai_risk: bool = False,
) -> dict:
    """Run the bot and materialize the basket as a saved Portfolio, then snapshot its NAV.
    Names the bot keeps carry their original entry price (cost-basis continuity), so the
    NAV curve stays meaningful across rebalances. With `ai`, attach an LLM risk review;
    with `apply_ai_risk`, deploy the AI-de-risked basket. Returns {signal, portfolio_id, snapshot}."""
    from pipeline_core.portfolio.models import (
        PortfolioBasis, PortfolioPosition, PortfolioSpec, Portfolio, WeightingScheme,
    )
    from pipeline_core.portfolio.track import snapshot_portfolio
    from pipeline_core.services import run_store

    signal = await generate_signal(top_n, universe, stop_pct, max_weight)
    if ai or apply_ai_risk:
        from pipeline_core.bots import ai as bot_ai
        signal = await bot_ai.annotate(signal, apply=apply_ai_risk)

    existing = await run_store.get_portfolio(name)
    prior_entry = {p.ticker: p.entry_price for p in existing.positions} if existing else {}

    positions = [
        PortfolioPosition(
            ticker=b["ticker"],
            weight=b["weight"],
            entry_price=prior_entry.get(b["ticker"], b["price"]),   # keep cost basis on holds
            stop_pct=stop_pct,
            stop_price=b["stop_price"],
            stop_source="bot",
            score=b["momentum_score"],
            rationale=f"Momentum {b['momentum_score']} · 90d {b['return_90d_pct']}% · vol {b['volatility_pct']}%",
        )
        for b in signal["basket"]
    ]
    spec = PortfolioSpec(
        name=name, market=MARKET, basis=PortfolioBasis.SYSTEMATIC,
        strategy_ids=[BOT_ID], weighting=WeightingScheme.SCORE,
    )
    pf = Portfolio(
        id=existing.id if existing else None,
        name=name, spec=spec, positions=positions, cash_pct=signal["cash_pct"],
        summary={"bot": BOT_ID, "regime": signal["regime"], "position_count": len(positions),
                 "invested_pct": round(sum(p.weight for p in positions), 4),
                 "ai_conviction": (signal.get("ai") or {}).get("conviction")},
        disclaimer=signal["disclaimer"],
    )
    saved = await run_store.save_portfolio(pf)
    snap = await snapshot_portfolio(saved)
    return {"signal": signal, "portfolio_id": saved.id, "snapshot": snap}
