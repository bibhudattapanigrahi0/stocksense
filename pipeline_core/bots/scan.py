"""
Bot scanner — run a signal bot across the crypto universe on a chosen timeframe and
return the coins signaling **long or short right now**, each with side + entry / stop /
target / R.

For multi-timeframe bots it also loads each coin's higher timeframe and BTC's higher
timeframe (fetched once) as context. Bounded concurrency; per-coin failures are skipped.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from pipeline_core.bots.base import get_bot, htf_for
from pipeline_core.config import settings
from pipeline_core.markets import get_universe_tickers
from pipeline_core.services import crypto_data

logger = logging.getLogger(__name__)

_DISCLAIMER = ("Decision-support research, not investment advice. Intraday crypto is volatile; "
               "signals are live snapshots — size positions with the given stop and manage risk.")


async def scan(
    bot_id: str,
    timeframe: str = "15m",
    universe: str = "Top 50",
    bars: int = 300,
    limit: int = 25,
) -> dict:
    """Scan `universe` with signal bot `bot_id` on `timeframe`; return coins now long."""
    bot = get_bot(bot_id)
    if bot is None:
        return {"error": f"Unknown bot '{bot_id}'", "bot": bot_id}
    if bot.kind != "signal":
        return {"error": f"'{bot_id}' is a {bot.kind} bot, not a scannable signal bot.", "bot": bot_id}
    if timeframe not in bot.timeframes:
        timeframe = bot.default_timeframe

    htf = htf_for(timeframe) if bot.needs_htf else None
    symbols = await asyncio.to_thread(get_universe_tickers, "CRYPTO", universe)

    # BTC context (higher timeframe) — fetched once for the whole scan.
    btc_df = None
    if bot.needs_btc:
        btc_df = await asyncio.to_thread(crypto_data.get_klines, "BTC", htf or timeframe, 200)

    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def load(sym: str):
        async with sem:
            try:
                df = await asyncio.to_thread(crypto_data.get_klines, sym, timeframe, bars)
                htf_df = await asyncio.to_thread(crypto_data.get_klines, sym, htf, 200) if htf else None
                return sym, df, htf_df
            except Exception as e:
                logger.warning(f"scan load failed for {sym}: {e}")
                return sym, None, None

    loaded = await asyncio.gather(*(load(s) for s in symbols))

    signaling, scanned = [], 0
    for sym, df, htf_df in loaded:
        if df is None or df.empty:
            continue
        scanned += 1
        ctx = {"htf_df": htf_df, "btc_df": btc_df, "timeframe": timeframe}
        try:
            sig = bot.evaluate(df, ctx)
        except Exception as e:
            logger.warning(f"evaluate failed for {sym}: {e}")
            continue
        if sig.get("side") in ("long", "short"):
            signaling.append({
                "ticker": sym, "side": sig["side"], "price": sig["entry"], "entry": sig["entry"],
                "stop": sig["stop"], "target": sig["target"], "r_multiple": sig["r_multiple"],
                "reason": sig["reason"], "indicators": sig["indicators"],
            })

    # Best reward:risk first.
    signaling.sort(key=lambda s: (s["r_multiple"] or 0), reverse=True)
    n_long = sum(1 for s in signaling if s["side"] == "long")
    n_short = len(signaling) - n_long

    note = f"{n_long} long / {n_short} short of {scanned} scanned coins on {timeframe}"
    if htf:
        note += f" (gated by the {htf} trend" + (" + BTC regime" if bot.needs_btc else "") + ")"

    return {
        "bot": bot_id, "bot_name": bot.name, "timeframe": timeframe, "htf": htf,
        "universe": universe, "as_of": datetime.now(timezone.utc).isoformat(),
        "scanned": scanned, "count": len(signaling),
        "long": n_long, "short": n_short, "signaling": signaling[:limit],
        "note": note, "disclaimer": _DISCLAIMER,
    }
