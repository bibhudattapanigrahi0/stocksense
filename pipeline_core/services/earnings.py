"""
Earnings-event awareness — next scheduled earnings date (live) + the historical date series.

Event risk is a real accuracy leak: a technical setup entered right before a binary earnings print
is a coin-flip. This service exposes:
  • `days_to_earnings` — for the live idea board's soft rank penalty + a ⚠️ flag as a report nears.
  • `earnings_dates`   — the full historical schedule (yfinance goes back ~15-20y), so a backtest can
                         avoid entering in the pre-earnings window.

yfinance-backed and cached; any failure degrades to None / [] so callers simply treat the name as
having no known event (never raises, never blocks the idea pipeline).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import diskcache

from pipeline_core.config import CACHE_DIR

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "earnings"))

TTL_OK = 12 * 3600      # the schedule shifts rarely — refresh twice a day
TTL_MISS = 3600         # brief negative cache so a transient failure retries within the hour


def _all_dates(symbol: str, market: str = "IN") -> list[str]:
    """Every known earnings date (past + upcoming estimates) as ISO strings, cached. [] on failure."""
    ck = f"earn:{market}:{symbol}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    import yfinance as yf   # lazy — keep import graph shallow / offline-friendly
    from pipeline_core.markets import normalize_ticker
    try:
        df = yf.Ticker(normalize_ticker(symbol, market)).get_earnings_dates(limit=60)
        dates = sorted({ts.date().isoformat() for ts in df.index}) if df is not None and len(df) else []
        cache.set(ck, dates, expire=TTL_OK if dates else TTL_MISS)
        return dates
    except Exception as e:
        logger.warning("earnings fetch failed for %s/%s: %s", symbol, market, e)
        cache.set(ck, [], expire=TTL_MISS)
        return []


def next_earnings_date(symbol: str, market: str = "IN") -> Optional[str]:
    """The next scheduled earnings date (ISO) on or after today, or None if unknown."""
    today = date.today()
    future = [d for d in _all_dates(symbol, market) if date.fromisoformat(d) >= today]
    return future[0] if future else None


def days_to_earnings(symbol: str, market: str = "IN") -> Optional[int]:
    """Calendar days until the next earnings report, or None if unknown."""
    nxt = next_earnings_date(symbol, market)
    return (date.fromisoformat(nxt) - date.today()).days if nxt else None


def earnings_dates(symbol: str, market: str = "IN") -> list[date]:
    """All known earnings dates (past + upcoming) as `date` objects — for backtest windows."""
    return [date.fromisoformat(d) for d in _all_dates(symbol, market)]
