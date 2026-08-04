"""
Backtest data loader — long OHLCV history for the whole universe.

Historical daily bars are effectively immutable, so the 10y frame is cached
for 20 hours (refreshing picks up the latest completed session); a failed
symbol degrades to "skipped" and is counted, never fatal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import diskcache
import pandas as pd
import yfinance as yf

from pipeline_core.config import CACHE_DIR, settings
from pipeline_core.markets import get_market, normalize_ticker

logger = logging.getLogger(__name__)
_bt_cache = diskcache.Cache(str(CACHE_DIR / "backtest"))

BT_CACHE_TTL = 20 * 3600  # 20 h — history is immutable; refresh adds the latest session
MIN_BARS = 300            # stocks with less history are skipped (recent listings)


def get_long_history(symbol: str, years: int = 10, market: str = "IN") -> pd.DataFrame:
    """10y daily OHLCV for one symbol (cached, market-scoped)."""
    m = get_market(market)
    key = f"bt:{m.key}:{symbol}:{years}y"
    cached = _bt_cache.get(key)
    if cached is not None:
        return cached

    # Crypto long history comes from Binance (full coverage of liquid coins); fall
    # through to yfinance for any coin Binance doesn't list.
    if m.key == "CRYPTO":
        from pipeline_core.services import crypto_data

        cdf = crypto_data.get_ohlcv(symbol, years * 365 + 30)
        if not cdf.empty:
            _bt_cache.set(key, cdf, expire=BT_CACHE_TTL)
            return cdf

    try:
        df = yf.Ticker(normalize_ticker(symbol, m.key)).history(period=f"{years}y", interval="1d", auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        df = df.dropna(subset=["Close"])
        # yfinance returns tz-aware index (Asia/Kolkata) — normalize to naive
        # so trade dates (naive) align for slicing and arithmetic.
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        _bt_cache.set(key, df, expire=BT_CACHE_TTL)
        return df
    except Exception as e:
        logger.error(f"Backtest history failed for {symbol}: {e}")
        return pd.DataFrame()


async def load_universe_history(
    tickers: list[str],
    years: int = 10,
    concurrency: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    market: str = "IN",
) -> tuple[dict[str, pd.DataFrame], dict]:
    """
    Bounded-concurrency load of long history for all tickers.
    Returns ({ticker: df}, stats) where stats tracks skipped/failed counts.
    """
    limit = concurrency or settings.ingestion_concurrency
    semaphore = asyncio.Semaphore(limit)
    total = len(tickers)
    done = 0
    stats = {"total": total, "loaded": 0, "skipped_insufficient": 0, "failed": 0}

    async def _one(ticker: str):
        nonlocal done
        async with semaphore:
            try:
                df = await asyncio.to_thread(get_long_history, ticker, years, market)
                if df.empty:
                    stats["failed"] += 1
                    return ticker, None
                if len(df) < MIN_BARS:
                    stats["skipped_insufficient"] += 1
                    return ticker, None
                stats["loaded"] += 1
                return ticker, df
            except Exception as e:
                logger.error(f"History load error {ticker}: {e}")
                stats["failed"] += 1
                return ticker, None
            finally:
                done += 1
                if progress_callback:
                    progress_callback(done, total)

    pairs = await asyncio.gather(*(_one(t) for t in tickers))
    result = {t: df for t, df in pairs if df is not None}
    logger.info(f"Backtest history: {stats}")
    return result, stats


async def load_benchmark(symbol: str = "^NSEI", years: int = 10, market: str = "IN") -> pd.DataFrame:
    """Benchmark index history for the same window."""
    return await asyncio.to_thread(get_long_history, symbol, years, market)
