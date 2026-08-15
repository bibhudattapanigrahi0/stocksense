"""
Market-regime gauge — the per-market risk-on / risk-off tide for the daily trade ideas.

Where ``market_sentiment.fear_greed()`` reads the *crypto* market's mood, this reads *any*
market's **regime**: is the benchmark trending up or down, and is volatility calm or stressed?
It returns a signed ``tilt ∈ [-1, +1]`` (−1 = deep risk-off … +1 = strong risk-on) that the daily
idea board uses to (a) rank regime-aligned longs first and (b) trim position size when the tide is
against them — "trade with the market", the biggest cross-market accuracy lever the daily side was
missing (the benchmark was only ever used for beta/alpha, never to gate ideas).

Per market:
  • **Equity (IN / US)** — benchmark 200-DMA trend + the vol index (India VIX / VIX) level & 1y percentile.
  • **Metals**          — Gold 200-DMA trend + the US Dollar Index (DXY) trend, **inverted** (a strong
                          dollar is a metals headwind).
  • **Crypto**          — BTC 200-DMA trend blended with the existing crypto **Fear & Greed** reading.

Kept deliberately defensive, like ``market_sentiment``: every price fetch is cached (via
``market_data``, 15-min TTL) and any failure degrades to a **neutral ``tilt=0`` reading**, so the
idea pipeline is never blocked and an offline run reproduces the ungated board exactly. The whole
gauge is cached ~1h via diskcache.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import diskcache

from pipeline_core.agents import indicators as ind
from pipeline_core.config import CACHE_DIR, settings
from pipeline_core.markets import Market, get_market
from pipeline_core.utils import round_price

logger = logging.getLogger(__name__)

_cache = diskcache.Cache(str(CACHE_DIR / "market_regime"))

# ── Scoring constants (documented; all thresholds live here) ────────
# Score is 0-100 with 50 = neutral; tilt = (score-50)/50.
_TREND_UP, _TREND_DOWN = 20.0, -20.0        # benchmark 200-DMA trend contribution
# Equity vol-index bands (VIX / India VIX both sit ~10-15 calm, ~20 elevated, >28 stressed).
_VOL_CALM, _VOL_ELEVATED, _VOL_HIGH = 16.0, 22.0, 28.0
_VOL_CONTRIB = {"calm": 15.0, "normal": 0.0, "elevated": -15.0, "high": -28.0}
_DXY_CONTRIB = 15.0                          # metals: dollar trend contribution (applied inverted)
_FNG_WEIGHT = 0.5                            # crypto: (fear_greed - 50) * this
_RISK_ON, _RISK_OFF = 60.0, 40.0            # score bands → state label
# Equity macro risk-appetite confirmers (modest, so the benchmark trend + vol still lead — the
# reading stays sharp, not a 6-way mush). US instruments, relevant to any equity via global flows.
_CREDIT_CONTRIB = 8.0                        # HYG/LQD credit-spread trend (firm credit = risk-on)
_RATES_CONTRIB = 5.0                         # 10Y yield trend (rising rates = equity headwind)
_DOLLAR_CONTRIB = 5.0                        # DXY trend for equities (strong dollar = headwind)

_VOL_LABELS = {"^VIX": "VIX", "^INDIAVIX": "India VIX"}


def _neutral(m: Market, *, detail: str = "unavailable") -> dict:
    """The reading returned whenever the regime can't be measured — tilt 0 ⇒ no ranking/size effect,
    so the board is identical to the ungated one."""
    return {
        "market": m.key, "state": "neutral", "score": 50.0, "tilt": 0.0,
        "benchmark": {"label": m.benchmark_label, "trend": None, "detail": detail},
        "volatility": None, "factors": [], "as_of": None, "stale": True,
    }


def _history(symbol: str, market: str):
    """Daily close series for a symbol via the shared, cached market-data service, or None on any
    failure. `^`-prefixed indices and futures symbols pass through `normalize_ticker` untouched."""
    from pipeline_core.services import market_data  # lazy: keep import graph shallow

    try:
        df = market_data.get_price_history(symbol, period="1y", interval="1d", market=market)
        if df is None or df.empty or "Close" not in df:
            return None
        return df["Close"].astype(float)
    except Exception as e:
        logger.warning(f"regime history fetch failed for {symbol}/{market}: {e}")
        return None


def _trend(close) -> tuple[Optional[bool], str]:
    """(is_uptrend, detail) from close vs its 200-DMA (100/50-DMA fallback on short history) — the
    same trend test the crypto momentum bot uses. (None, …) when there isn't enough history."""
    if close is None or len(close) == 0:
        return None, "no data"
    last = float(close.iloc[-1])
    for length in (200, 100, 50):
        ma = ind.sma(close, length)
        val = ma.iloc[-1]
        if val == val:  # not NaN
            up = last > float(val)
            return up, f"{round_price(last)} {'>' if up else '≤'} {length}-DMA {round_price(float(val))}"
    return None, "insufficient history"


# ── Backtest-time regime series (historical risk-on/off for the crypto strategy filter) ──────────
# The live gauge above returns a single point-in-time tilt. The backtest instead needs the FULL daily
# history of the benchmark's regime so a per-coin entry/exit signal can be gated on the market-wide tide
# — crypto's dominant risk factor is BTC, and a per-coin filter lets you buy an alt right before the whole
# market rolls over. Reuses the same long-MA trend concept as `_trend`. Anti-lookahead: the regime at each
# date uses only benchmark data up to that date, then is shifted one bar (yesterday's regime gates today's
# entry, which itself executes next open).


def regime_series(benchmark_close):
    """Daily risk-on (True) / risk-off (False) from the benchmark's long-MA trend: close above a
    *rising* long moving average. 200-DMA where history allows, 100/50 fallback for a short series
    (mirrors `_trend`). Returns a boolean Series (tz-naive, sorted) shifted one bar so it never uses
    same-day information; pre-history defaults to risk-on. None on empty input."""
    if benchmark_close is None or len(benchmark_close) == 0:
        return None
    close = benchmark_close.dropna().astype(float)
    if getattr(close.index, "tz", None) is not None:
        close = close.tz_localize(None)
    close = close.sort_index()
    n = len(close)
    length = 200 if n >= 200 else 100 if n >= 100 else 50 if n >= 50 else max(2, n // 2)
    ma = ind.sma(close, length)
    risk_on = (close > ma) & (ma > ma.shift(20))
    # Shift one bar for strict anti-lookahead; pre-history (NaN MA) defaults to risk-on (never blocks).
    return risk_on.shift(1).fillna(True).astype(bool)


def enrich_frames_with_market(df_by_ticker: dict, benchmark_close) -> None:
    """Inject a boolean ``MKT_RISK_ON`` column (the benchmark's daily regime from `regime_series`) onto
    every frame, so market-gated strategies can read the market-wide tide inside a single-coin entry/exit
    signal. Forward-filled onto each frame's dates (risk-on before the series starts). Mutates in place;
    never raises — a failure just leaves frames without the column and strategies fall back to their
    per-coin proxy. Mirrors ``smart_money.enrich_frames``."""
    try:
        series = regime_series(benchmark_close)
        if series is None or series.empty:
            return
        for df in df_by_ticker.values():
            if df is None or getattr(df, "empty", True):
                continue
            idx = df.index
            key = idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx
            df["MKT_RISK_ON"] = series.reindex(key, method="ffill").fillna(True).to_numpy().astype(bool)
    except Exception as e:
        logger.warning(f"market-regime frame enrichment skipped: {e}")


def _percentile(close, value: float) -> float:
    """Fraction of the trailing series ≤ `value` (0-1). Used to contextualize the vol reading."""
    s = close.dropna()
    return float((s <= value).mean()) if len(s) else 0.5


def _vol_level(val: float) -> str:
    if val < _VOL_CALM:
        return "calm"
    if val < _VOL_ELEVATED:
        return "normal"
    if val < _VOL_HIGH:
        return "elevated"
    return "high"


def _state(score: float) -> str:
    if score >= _RISK_ON:
        return "risk-on"
    if score <= _RISK_OFF:
        return "risk-off"
    return "neutral"


def _compute(m: Market) -> dict:
    """Build the live reading for one market. Each factor degrades independently to a 0 contribution
    (flagging `stale`) rather than raising, so a partial outage still yields a well-formed reading."""
    score = 50.0
    factors: list[str] = []
    stale = False

    # 1. Benchmark 200-DMA trend (all markets).
    up, detail = _trend(_history(m.benchmark, m.key))
    if up is None:
        stale = True
        benchmark = {"label": m.benchmark_label, "trend": None, "detail": "no data"}
    else:
        score += _TREND_UP if up else _TREND_DOWN
        benchmark = {"label": m.benchmark_label, "trend": "up" if up else "down", "detail": detail}
        factors.append(f"{m.benchmark_label} {detail}")

    volatility: Optional[dict] = None

    # 2a. Equity volatility index (IN / US).
    if m.vol_symbol:
        vclose = _history(m.vol_symbol, m.key)
        if vclose is not None and len(vclose):
            val = float(vclose.iloc[-1])
            level = _vol_level(val)
            score += _VOL_CONTRIB[level]
            label = _VOL_LABELS.get(m.vol_symbol, "Volatility")
            volatility = {"label": label, "value": round(val, 1), "level": level,
                          "percentile": round(_percentile(vclose, val) * 100)}
            factors.append(f"{label} {round(val, 1)} ({level})")
        else:
            stale = True

    # 2c. Equity macro risk-appetite — rates / credit / dollar, layered on the benchmark trend + vol
    # as global confirmers. Kept modest so aligned signals SHARPEN the tilt (strong trend + firm
    # credit → a stronger read) while the benchmark still leads. US instruments (relevant to any
    # equity via global flows); missing data contributes 0 and never flags stale — these only refine.
    if m.kind == "equity":
        hyg, lqd = _history("HYG", "US"), _history("LQD", "US")   # high-yield vs investment-grade
        if hyg is not None and lqd is not None and len(hyg) and len(lqd):
            cup, _ = _trend((hyg / lqd).dropna())                 # ratio rising = credit firm = risk-on
            if cup is not None:
                score += _CREDIT_CONTRIB if cup else -_CREDIT_CONTRIB
                factors.append("credit firm" if cup else "credit stressed")
        rup, _ = _trend(_history("^TNX", "US"))                    # 10Y yield: rising = equity headwind
        if rup is not None:
            score += -_RATES_CONTRIB if rup else _RATES_CONTRIB
            factors.append("10Y rising" if rup else "10Y easing")
        dup, _ = _trend(_history("UUP", "US"))                     # UUP (dollar ETF) — strong $ = headwind
        if dup is not None:                                        # (the DXY index symbol DX-Y.NYB gets
            score += -_DOLLAR_CONTRIB if dup else _DOLLAR_CONTRIB  # mangled by the US '.'→'-' normalizer)
            factors.append("USD strong" if dup else "USD soft")

    # 2b. Metals macro driver — DXY trend, inverted (strong dollar = metals headwind).
    if m.macro_symbol:
        d_up, d_detail = _trend(_history(m.macro_symbol, m.key))
        if d_up is None:
            stale = True
        else:
            score += -_DXY_CONTRIB if d_up else _DXY_CONTRIB
            factors.append(f"DXY {d_detail} → {'headwind' if d_up else 'tailwind'}")

    # 2c. Crypto — blend the existing market-wide Fear & Greed reading.
    if m.key == "CRYPTO":
        from pipeline_core.services import market_sentiment  # lazy: shared crypto sentiment entrypoint

        fng = market_sentiment.fear_greed()
        if fng and not fng.get("stale"):
            score += (fng["value"] - 50) * _FNG_WEIGHT
            volatility = {"label": "Fear & Greed", "value": fng["value"],
                          "level": fng.get("classification"), "percentile": fng["value"]}
            factors.append(f"Fear & Greed {fng['value']} ({fng.get('classification')})")
        else:
            stale = True

    score = max(0.0, min(100.0, score))
    return {
        "market": m.key, "state": _state(score), "score": round(score, 1),
        "tilt": round((score - 50.0) / 50.0, 3),
        "benchmark": benchmark, "volatility": volatility, "factors": factors,
        "as_of": datetime.now(timezone.utc).isoformat(), "stale": stale,
    }


# ── Per-idea regime fit (how an idea's strategy families relate to the tide) ──
# Pro-cyclical families chase strength (long breakouts/trends) → hurt in risk-off, helped in
# risk-on; counter-cyclical families buy weakness (oversold/contrarian) → the reverse. This is what
# lets a uniform market tilt REORDER a long-only board: it pulls each idea by its own cyclicality.
_CYCLICALITY = {
    "Trend": 1.0, "Momentum": 1.0, "Breakout": 1.0, "Volume": 1.0,
    "Mean Reversion": -1.0, "Contrarian": -1.0,
}


def idea_cyclicality(categories) -> float:
    """Mean pro-/counter-cyclical score in [-1, +1] for the strategy families behind an idea
    (+1 = pure trend/momentum/breakout, −1 = pure mean-reversion/contrarian, 0 = mixed/unknown, e.g.
    a carried idea that no longer carries its categories)."""
    scores = [_CYCLICALITY[c] for c in (categories or []) if c in _CYCLICALITY]
    return sum(scores) / len(scores) if scores else 0.0


def market_regime(market: Optional[str] = None) -> dict:
    """Latest risk-on/off regime for a market: ``{state, score 0-100, tilt -1..+1, benchmark,
    volatility, factors, as_of, stale}``.

    Cached ~1h. Any hard failure returns a **neutral ``tilt=0``** reading (``stale=True``) — callers
    get a well-formed dict unconditionally and a neutral tilt leaves the idea board unchanged.
    """
    m = get_market(market)
    cache_key = f"regime:{m.key}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return dict(cached)
    try:
        reading = _compute(m)
        _cache.set(cache_key, reading, expire=settings.cache_ttl_regime)
        return dict(reading)
    except Exception as e:  # never block the idea pipeline on the gauge
        logger.warning(f"market regime for {m.key} failed, using neutral: {e}")
        return _neutral(m)
