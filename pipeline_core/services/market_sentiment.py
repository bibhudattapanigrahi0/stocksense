"""
Market-wide sentiment — the **Fear & Greed** gauge, per market.

A single macro risk-on / risk-off reading (0 = extreme fear … 100 = extreme greed) for a whole
market, so the daily idea board can pair it with the risk-on/off *direction* (`market_regime`) in a
"Market Conditions" header. Where per-coin/per-name news sentiment (`agents/sentiment.py`) reads one
ticker's headlines, this reads the *market's mood*, and idea curation leans long into greed / short
into fear (with a contrarian nudge at the extremes — see `bots/selection.py`).

Because no single free source covers every market, ``fear_greed(market)`` dispatches by market:
  • **CRYPTO** — the free alternative.me Fear & Greed feed (no key), the original source.
  • **US**     — the real **CNN Fear & Greed Index** via its keyless (unofficial) dataviz endpoint.
  • **IN / other** — a **derived "Market Mood"**: the benchmark's momentum vs its ~125-DMA blended
                     with an inverted volatility-index percentile (calm ⇒ greed). Labelled *derived*
                     so it never masquerades as an official index (India has no free canonical one).

Kept deliberately defensive, like `agents/sentiment.py::_fetch_news`: every network/price call is
timeout-bounded and cached, and any failure (offline, rate-limited, schema drift, thin history)
degrades to a **neutral 50** so the idea pipeline never blocks on it. Cached ~1h via diskcache.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import diskcache

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)

_cache = diskcache.Cache(str(CACHE_DIR / "fear_greed"))
_CACHE_KEY = "crypto:fng:latest"
_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"
# CNN's Fear & Greed Index — unofficial but keyless dataviz endpoint (needs a browser UA).
_CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CNN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


def _neutral(*, source: str, label: str) -> dict:
    """The reading returned whenever a source is unavailable — 50 = "no macro tilt", so curation
    falls back to pure edge/agreement without a sentiment bias. Carries the attempted source/label so
    the UI still labels the (stale) gauge correctly."""
    return {"value": 50, "classification": "Neutral", "as_of": None, "stale": True,
            "source": source, "label": label}


def _close_series(symbol: str, market: str):
    """Daily close series for a symbol via the shared cached market-data service, or None on any
    failure (mirrors ``market_regime._history``; kept local to avoid an import cycle)."""
    from pipeline_core.services import market_data  # lazy: keep the import graph shallow

    try:
        df = market_data.get_price_history(symbol, period="1y", interval="1d", market=market)
        if df is None or df.empty or "Close" not in df:
            return None
        return df["Close"].astype(float)
    except Exception as e:
        logger.warning(f"fear-greed history fetch failed for {symbol}/{market}: {e}")
        return None


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


def fear_greed(market: str = "CRYPTO") -> dict:
    """Latest Fear & Greed reading for a market: ``{value 0-100, classification, as_of, stale,
    source, label}`` (derived readings also carry ``factors``).

    Dispatches by market — CRYPTO ⇒ alternative.me, US ⇒ the CNN index (unless
    ``fear_greed_cnn_enabled`` is off), everything else ⇒ a derived "Market Mood". Each path is
    network/price-bounded and cached ~1h, and any failure returns a **neutral 50** reading
    (``stale=True``) so callers get a well-formed dict unconditionally — they never handle an
    exception. The no-arg default keeps every existing crypto caller working unchanged.
    """
    m = (market or "CRYPTO").upper()
    if m == "CRYPTO":
        return _crypto_fear_greed()
    if m == "US" and settings.fear_greed_cnn_enabled:
        return _cnn_fear_greed()
    return _derived_fear_greed(m)


def _crypto_fear_greed() -> dict:
    """Crypto market mood from the free alternative.me Fear & Greed feed (no key)."""
    cached = _cache.get(_CACHE_KEY)
    if cached is not None:
        reading = dict(cached)
        reading.setdefault("source", "crypto")        # tolerate entries cached before these fields
        reading.setdefault("label", "Fear & Greed")   # existed (the crypto cache key is unchanged)
        return reading

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
            "stale": False, "source": "crypto", "label": "Fear & Greed",
        }
        _cache.set(_CACHE_KEY, reading, expire=settings.cache_ttl_fear_greed)
        return dict(reading)
    except Exception as e:                                      # offline / rate-limited / schema drift
        logger.warning(f"crypto Fear & Greed fetch failed, using neutral 50: {e}")
        return _neutral(source="crypto", label="Fear & Greed")


def _cnn_fear_greed() -> dict:
    """US market mood from CNN's Fear & Greed Index (keyless, unofficial dataviz endpoint)."""
    ck = "us:cnn:fng:latest"
    cached = _cache.get(ck)
    if cached is not None:
        return dict(cached)

    import requests

    try:
        resp = requests.get(_CNN_URL, headers=_CNN_HEADERS, timeout=10)
        resp.raise_for_status()
        fg = (resp.json() or {}).get("fear_and_greed", {})
        value = int(round(float(fg["score"])))                 # 0-100 float
        value = max(0, min(100, value))
        rating = (fg.get("rating") or "").strip()
        reading = {
            "value": value,
            "classification": rating.title() if rating else _classify(value),
            "as_of": fg.get("timestamp"),
            "stale": False, "source": "cnn", "label": "Fear & Greed",
        }
        _cache.set(ck, reading, expire=settings.cache_ttl_fear_greed)
        return dict(reading)
    except Exception as e:                                      # offline / blocked / schema drift
        logger.warning(f"CNN Fear & Greed fetch failed, using neutral 50: {e}")
        return _neutral(source="cnn", label="Fear & Greed")


def _derived_fear_greed(market: str) -> dict:
    """Derived "Market Mood" for a market with no free canonical index (e.g. IN): the benchmark's
    momentum vs its ~125-DMA blended with an inverted volatility-index percentile (calm ⇒ greed).
    Labelled *derived* — a real composite, but not an official index. Neutral 50 if history is thin.
    """
    from pipeline_core.agents import indicators as ind        # lazy: keep the import graph shallow
    from pipeline_core.markets import get_market

    ck = f"derived:fng:{market}"
    cached = _cache.get(ck)
    if cached is not None:
        return dict(cached)

    try:
        m = get_market(market)
    except Exception:
        return _neutral(source="derived", label="Market Mood")

    subs: list[tuple[float, float]] = []                       # (sub-score 0-100, weight)
    factors: list[str] = []

    # 1. Momentum — benchmark last close vs its 125-DMA, mapped [-10%, +10%] → [0, 100] (50 = at MA).
    close = _close_series(m.benchmark, m.key)
    if close is not None and len(close):
        last = float(close.iloc[-1])
        ma = ind.sma(close, 125)
        val = float(ma.iloc[-1]) if len(ma) else float("nan")
        if val == val and val:                                # not NaN, non-zero
            pct = last / val - 1.0
            mom = max(0.0, min(100.0, 50.0 + (pct / 0.10) * 50.0))
            subs.append((mom, settings.fng_w_momentum))
            factors.append(f"{m.benchmark_label} {'+' if pct >= 0 else ''}{round(pct * 100, 1)}% vs 125-DMA")

    # 2. Volatility — vol-index percentile over 1y, inverted (elevated vol ⇒ fear).
    if m.vol_symbol:
        vclose = _close_series(m.vol_symbol, m.key)
        if vclose is not None:
            v = vclose.dropna()
            if len(v):
                pctile = float((v <= float(v.iloc[-1])).mean())   # 0..1, high = elevated vol
                vol_mood = max(0.0, min(100.0, (1.0 - pctile) * 100.0))
                subs.append((vol_mood, settings.fng_w_volatility))
                factors.append(f"vol {round(float(v.iloc[-1]), 1)} @ {round(pctile * 100)}%ile")

    if not subs:
        return _neutral(source="derived", label="Market Mood")

    wsum = sum(w for _, w in subs)
    value = int(round(sum(s * w for s, w in subs) / wsum)) if wsum else 50
    value = max(0, min(100, value))
    reading = {
        "value": value, "classification": _classify(value),
        "as_of": datetime.now(timezone.utc).isoformat(), "stale": False,
        "source": "derived", "label": "Market Mood", "factors": factors,
    }
    _cache.set(ck, reading, expire=settings.cache_ttl_fear_greed)
    return dict(reading)


_coin_news_cache = diskcache.Cache(str(CACHE_DIR / "crypto_news_sentiment"))


async def coin_news(ticker: str, name: Optional[str] = None) -> dict:
    """Per-coin news sentiment for the crypto market — a thin async wrapper over the existing
    news-sentiment agent, so idea curation has a **single sentiment entrypoint** (this module).
    Returns the agent's ``{score 0-100, consensus, summary, method, ...}``; degrades to a neutral
    50 on any failure. **Result-cached ~1h** (like ``news_sentiment`` below), because the intraday
    idea board re-scores the same candidate coins on every load — without this, each request re-hits
    the LLM for up to a dozen coins (and, when the provider is down, eats a per-coin retry). Routed
    here so the hermetic test fixture can stub one place.
    """
    from pipeline_core.agents.sentiment import analyze_ticker_sentiment  # lazy: avoid import cycle

    ck = f"coin_news:{ticker.upper()}"
    hit = _coin_news_cache.get(ck)
    if hit is not None:
        return dict(hit)

    try:
        result = await analyze_ticker_sentiment(ticker, name, market="CRYPTO")
    except Exception as e:
        logger.warning(f"coin news sentiment failed for {ticker}, neutral: {e}")
        result = {"score": 50.0, "consensus": "Neutral", "summary": "", "method": "unavailable"}
    if result.get("method") != "unavailable":       # don't cache a transient failure
        _coin_news_cache.set(ck, result, expire=settings.cache_ttl_news)
    return dict(result)


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
