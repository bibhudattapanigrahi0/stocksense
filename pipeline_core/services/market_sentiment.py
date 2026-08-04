"""
Market-wide crypto sentiment — the **Fear & Greed Index**.

A single macro risk-on / risk-off gauge (0 = extreme fear … 100 = extreme greed) for the whole
crypto market, from the free alternative.me feed (no API key). Where the per-coin news sentiment
(`agents/sentiment.py`) reads one ticker's headlines, this reads the *market's mood*, so idea
curation can lean long into greed / short into fear (with a contrarian nudge at the extremes —
see `bots/selection.py`).

Kept deliberately defensive, like `agents/sentiment.py::_fetch_news`: the HTTP call is
timeout-bounded and any failure (offline, rate-limited, schema drift) degrades to a **neutral 50**
so the idea pipeline never blocks on it. Cached ~1h via diskcache.
"""

from __future__ import annotations

import logging
from typing import Optional

import diskcache

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)

_cache = diskcache.Cache(str(CACHE_DIR / "fear_greed"))
_CACHE_KEY = "crypto:fng:latest"
_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"

# The neutral reading returned whenever the live feed is unavailable — 50 = "no macro tilt", so
# curation falls back to pure edge/agreement without a sentiment bias.
_NEUTRAL = {"value": 50, "classification": "Neutral", "as_of": None, "stale": True}


def _classify(value: int) -> str:
    """alternative.me's own bucketing, recomputed locally so a cached value stays labelled."""
    if value <= 24:
        return "Extreme Fear"
    if value <= 44:
        return "Fear"
    if value <= 55:
        return "Neutral"
    if value <= 74:
        return "Greed"
    return "Extreme Greed"


def fear_greed() -> dict:
    """Latest crypto Fear & Greed reading: ``{value 0-100, classification, as_of, stale}``.

    Network-bounded and cached ~1h. Any failure returns the neutral 50 reading (``stale=True``)
    so callers get a well-formed dict unconditionally — they never need to handle an exception.
    """
    cached = _cache.get(_CACHE_KEY)
    if cached is not None:
        return dict(cached)

    import requests

    try:
        resp = requests.get(_FNG_URL, headers={"User-Agent": "StockSense/2.0"}, timeout=10)
        resp.raise_for_status()
        row = (resp.json() or {}).get("data", [{}])[0]
        value = int(float(row["value"]))                       # feed sends the number as a string
        value = max(0, min(100, value))
        reading = {
            "value": value,
            "classification": row.get("value_classification") or _classify(value),
            "as_of": row.get("timestamp"),
            "stale": False,
        }
        _cache.set(_CACHE_KEY, reading, expire=settings.cache_ttl_fear_greed)
        return dict(reading)
    except Exception as e:                                      # offline / rate-limited / schema drift
        logger.warning(f"Fear & Greed fetch failed, using neutral 50: {e}")
        return dict(_NEUTRAL)


async def coin_news(ticker: str, name: Optional[str] = None) -> dict:
    """Per-coin news sentiment for the crypto market — a thin async wrapper over the existing
    news-sentiment agent, so idea curation has a **single sentiment entrypoint** (this module).
    Returns the agent's ``{score 0-100, consensus, summary, method, ...}``; degrades to a neutral
    50 on any failure. Routed here so the hermetic test fixture can stub one place.
    """
    from pipeline_core.agents.sentiment import analyze_ticker_sentiment  # lazy: avoid import cycle

    try:
        return await analyze_ticker_sentiment(ticker, name, market="CRYPTO")
    except Exception as e:
        logger.warning(f"coin news sentiment failed for {ticker}, neutral: {e}")
        return {"score": 50.0, "consensus": "Neutral", "summary": "", "method": "unavailable"}


_news_result_cache = diskcache.Cache(str(CACHE_DIR / "news_sentiment"))


async def news_sentiment(ticker: str, name: Optional[str] = None, market: str = "IN") -> dict:
    """Per-name news sentiment for the daily idea board (any equity market) — a **result-cached**
    (~1h) wrapper over the news agent, so the 90s live re-price loop never re-fetches/re-scores.
    Returns ``{score 0-100 (50 neutral), consensus, ...}``; neutral 50 on any failure. Single
    entrypoint (the hermetic fixture stubs this one).
    """
    ck = f"news_sent:{(market or 'IN').upper()}:{ticker}"
    hit = _news_result_cache.get(ck)
    if hit is not None:
        return dict(hit)

    from pipeline_core.agents.sentiment import analyze_ticker_sentiment  # lazy: avoid import cycle

    try:
        result = await analyze_ticker_sentiment(ticker, name, market=market)
    except Exception as e:
        logger.warning(f"news sentiment failed for {ticker}/{market}, neutral: {e}")
        result = {"score": 50.0, "consensus": "Neutral", "summary": "", "method": "unavailable"}
    if result.get("method") != "unavailable":       # don't cache a transient failure
        _news_result_cache.set(ck, result, expire=settings.cache_ttl_news)
    return dict(result)
