"""
Sector relative strength — is an idea's sector leading or lagging the benchmark?

Rotation is a real, backtestable effect: names in a leading sector tend to keep outperforming, so
the daily board can lift ideas whose *sector* has momentum and trim those fighting a lagging group.
Each sector's trailing return is read from a liquid **sector proxy** (SPDR sector ETFs for US) and
compared to the benchmark's over the same window.

Cached ~1h; degrades to None (neutral factor) whenever a market has no sector map (only US today)
or the proxy/benchmark data is unavailable — so it never blocks or biases the board on bad data.
"""

from __future__ import annotations

import logging
from typing import Optional

import diskcache

from pipeline_core.config import CACHE_DIR

logger = logging.getLogger(__name__)
_cache = diskcache.Cache(str(CACHE_DIR / "sector"))
TTL = 3600
LOOKBACK = 63          # ~3 trading months

# yfinance GICS sector name → sector proxy. US = SPDR select-sector ETFs (deep, reliable history).
# Other markets have no reliable free sectoral series on yfinance yet → left unmapped (neutral).
SECTOR_PROXY: dict[str, dict[str, str]] = {
    "US": {
        "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
        "Energy": "XLE", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP",
        "Industrials": "XLI", "Basic Materials": "XLB", "Utilities": "XLU",
        "Real Estate": "XLRE", "Communication Services": "XLC",
    },
}


def has_sectors(market: str) -> bool:
    return (market or "IN").upper() in SECTOR_PROXY


def _trailing_return_pct(symbol: str, market: str, lookback: int) -> Optional[float]:
    from pipeline_core.services import market_data   # lazy: shallow import graph
    try:
        df = market_data.get_price_history(symbol, period="6mo", interval="1d", market=market)
    except Exception as e:
        logger.warning("sector return fetch failed for %s: %s", symbol, e)
        return None
    if df is None or df.empty or "Close" not in df:
        return None
    close = df["Close"].dropna()
    if len(close) <= lookback:
        return None
    return float(close.iloc[-1] / close.iloc[-lookback - 1] - 1.0) * 100.0


def sector_rs(sector: Optional[str], market: str = "IN", lookback_days: int = LOOKBACK) -> Optional[float]:
    """Sector's trailing return minus the benchmark's, in percentage points (>0 = leading). None if
    the market/sector isn't mapped or the data is unavailable. Cached ~1h."""
    m = (market or "IN").upper()
    proxy = SECTOR_PROXY.get(m, {}).get(sector or "")
    if not proxy:
        return None
    ck = f"secrs:{m}:{sector}:{lookback_days}"
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    from pipeline_core.markets import get_market
    sr = _trailing_return_pct(proxy, m, lookback_days)
    br = _trailing_return_pct(get_market(m).benchmark, m, lookback_days)
    rs = round(sr - br, 2) if sr is not None and br is not None else None
    if rs is not None:
        _cache.set(ck, rs, expire=TTL)
    return rs
