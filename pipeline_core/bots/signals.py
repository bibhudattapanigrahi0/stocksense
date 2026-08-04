"""
Intraday crypto signal bots — each a pure `evaluate(df, ctx) -> signal` over one coin's
OHLCV on a chosen timeframe. Every bot is **bidirectional**: it looks for a long setup
and its mirror-image short (crypto is freely shortable via perps/margin), and returns
whichever fires — `long` / `short` / `flat` — with an ATR-based stop and an R-multiple
target. They reuse the shared indicator library (agents/indicators.py) and self-register
into the bot registry on import.

  breakout        — Donchian break (up = long / down = short) on expanding volume
  mean-reversion  — RSI(2) + Bollinger extreme (oversold = long / overbought = short)
  ema-trend       — EMA9 vs EMA21 with ADX + DI confirmation (ride the trend either way)
  vwap-reclaim    — reclaim VWAP from below = long / lose VWAP from above = short
  mtf-confluence  — ★ trade only with the higher-TF trend + BTC regime; long the pullback
                    in an uptrend, short the failed bounce in a downtrend (flagship idea)
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from pipeline_core.agents import indicators as ind
from pipeline_core.bots.base import BotDef, make_signal, register
from pipeline_core.utils import round_price

TFS = ["5m", "10m", "15m", "30m", "1h", "4h"]
_MIN_BARS = 40


def _f(x) -> Optional[float]:
    """float or None (NaN-safe)."""
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ── Breakout ────────────────────────────────────────────────────────


def _breakout(df: pd.DataFrame, ctx: dict, n: int = 20, vol_mult: float = 1.5,
              atr_mult: float = 1.5, rr: float = 1.5) -> dict:
    if len(df) < _MIN_BARS:
        return make_signal("flat", reason="insufficient data")
    high, low, close, vol = df["High"], df["Low"], df["Close"], df["Volume"]
    price = _f(close.iloc[-1])
    hi = _f(ind.rolling_high(high, n).iloc[-1])                 # prior n-bar high
    lo = _f(ind.rolling_low(low, n).iloc[-1])                   # prior n-bar low
    avg_vol = _f(vol.rolling(n).mean().iloc[-1])
    v = _f(vol.iloc[-1])
    atrv = _f(ind.atr(high, low, close, 14).iloc[-1])
    if None in (price, hi, lo, avg_vol, v, atrv) or avg_vol <= 0:
        return make_signal("flat", reason="insufficient data")
    vx = round(v / avg_vol, 2)
    base = {"donchian_high": round_price(hi), "donchian_low": round_price(lo), "vol_x": vx,
            "atr": round_price(atrv)}

    if price > hi and v > vol_mult * avg_vol:                   # break UP → long
        stop = price - atr_mult * atrv
        return make_signal("long", price, stop, price + rr * (price - stop),
                           reason=f"Broke {n}-bar high on {vx}× volume", indicators=base)
    if price < lo and v > vol_mult * avg_vol:                   # break DOWN → short
        stop = price + atr_mult * atrv
        return make_signal("short", price, stop, price - rr * (stop - price),
                           reason=f"Broke {n}-bar low on {vx}× volume", indicators=base)
    return make_signal("flat", reason=f"inside {n}-bar range", indicators=base)


# ── Mean-reversion ──────────────────────────────────────────────────


def _mean_reversion(df: pd.DataFrame, ctx: dict, atr_mult: float = 1.5) -> dict:
    if len(df) < _MIN_BARS:
        return make_signal("flat", reason="insufficient data")
    high, low, close = df["High"], df["Low"], df["Close"]
    bb = ind.bollinger_bands(close, 20, 2.0)
    price = _f(close.iloc[-1])
    rsi2 = _f(ind.rsi(close, 2).iloc[-1])
    lower = _f(bb["lower"].iloc[-1])
    upper = _f(bb["upper"].iloc[-1])
    mid = _f(bb["mid"].iloc[-1])
    atrv = _f(ind.atr(high, low, close, 14).iloc[-1])
    if None in (price, rsi2, lower, upper, mid, atrv):
        return make_signal("flat", reason="insufficient data")
    base = {"rsi2": round(rsi2, 1), "bb_lower": round_price(lower),
            "bb_upper": round_price(upper), "bb_mid": round_price(mid)}

    if rsi2 < 10 and price < lower:                            # oversold → long, revert up to mean
        stop = price - atr_mult * atrv
        target = mid if mid > price else price + (price - stop)
        return make_signal("long", price, stop, target,
                           reason=f"Oversold bounce (RSI2 {round(rsi2, 1)}, below lower band)", indicators=base)
    if rsi2 > 90 and price > upper:                            # overbought → short, revert down to mean
        stop = price + atr_mult * atrv
        target = mid if mid < price else price - (stop - price)
        return make_signal("short", price, stop, target,
                           reason=f"Overbought fade (RSI2 {round(rsi2, 1)}, above upper band)", indicators=base)
    return make_signal("flat", reason=f"in range (RSI2 {round(rsi2, 1)})", indicators=base)


# ── EMA trend ───────────────────────────────────────────────────────


def _ema_trend(df: pd.DataFrame, ctx: dict, atr_mult: float = 1.5, rr: float = 2.0) -> dict:
    if len(df) < _MIN_BARS:
        return make_signal("flat", reason="insufficient data")
    high, low, close = df["High"], df["Low"], df["Close"]
    ax = ind.adx(high, low, close, 14)
    price = _f(close.iloc[-1])
    fast = _f(ind.ema(close, 9).iloc[-1])
    slow = _f(ind.ema(close, 21).iloc[-1])
    adxv = _f(ax["adx"].iloc[-1])
    pdi = _f(ax["plus_di"].iloc[-1])
    mdi = _f(ax["minus_di"].iloc[-1])
    atrv = _f(ind.atr(high, low, close, 14).iloc[-1])
    if None in (price, fast, slow, adxv, pdi, mdi, atrv):
        return make_signal("flat", reason="insufficient data")
    base = {"ema9": round_price(fast), "ema21": round_price(slow), "adx": round(adxv, 1)}

    if fast > slow and price > slow and adxv > 20 and pdi > mdi:        # uptrend → long
        stop = min(slow, price - atr_mult * atrv)
        return make_signal("long", price, stop, price + rr * (price - stop),
                           reason=f"Uptrend (EMA9>EMA21, ADX {round(adxv)})", indicators=base)
    if fast < slow and price < slow and adxv > 20 and mdi > pdi:        # downtrend → short
        stop = max(slow, price + atr_mult * atrv)
        return make_signal("short", price, stop, price - rr * (stop - price),
                           reason=f"Downtrend (EMA9<EMA21, ADX {round(adxv)})", indicators=base)
    return make_signal("flat", reason=f"no trend (ADX {round(adxv, 1)})", indicators=base)


# ── VWAP reclaim / breakdown ────────────────────────────────────────


def _vwap_reclaim(df: pd.DataFrame, ctx: dict, atr_mult: float = 1.2, rr: float = 1.5) -> dict:
    if len(df) < _MIN_BARS:
        return make_signal("flat", reason="insufficient data")
    high, low, close, vol = df["High"], df["Low"], df["Close"], df["Volume"]
    vw = ind.vwap(high, low, close, vol, 20)
    price = _f(close.iloc[-1])
    vw_now = _f(vw.iloc[-1])
    vw_prev = _f(vw.iloc[-2])
    close_prev = _f(close.iloc[-2])
    rsiv = _f(ind.rsi(close, 14).iloc[-1])
    atrv = _f(ind.atr(high, low, close, 14).iloc[-1])
    if None in (price, vw_now, vw_prev, close_prev, rsiv, atrv):
        return make_signal("flat", reason="insufficient data")
    base = {"vwap": round_price(vw_now), "rsi": round(rsiv, 1)}

    reclaimed = close_prev <= vw_prev and price > vw_now      # crossed back ABOVE VWAP
    brokedown = close_prev >= vw_prev and price < vw_now      # lost VWAP from ABOVE
    if reclaimed and rsiv > 50:
        stop = min(vw_now - 0.2 * atrv, price - atr_mult * atrv)
        return make_signal("long", price, stop, price + rr * (price - stop),
                           reason=f"Reclaimed VWAP with momentum (RSI {round(rsiv)})", indicators=base)
    if brokedown and rsiv < 50:
        stop = max(vw_now + 0.2 * atrv, price + atr_mult * atrv)
        return make_signal("short", price, stop, price - rr * (stop - price),
                           reason=f"Lost VWAP with momentum (RSI {round(rsiv)})", indicators=base)
    return make_signal("flat", reason="no VWAP cross", indicators=base)


# ── Multi-timeframe confluence (flagship) ───────────────────────────


def _mtf_confluence(df: pd.DataFrame, ctx: dict, atr_mult: float = 1.5, rr: float = 2.0) -> dict:
    htf, btc = ctx.get("htf_df"), ctx.get("btc_df")
    if len(df) < _MIN_BARS or htf is None or len(htf) < 55 or btc is None or len(btc) < 15:
        return make_signal("flat", reason="insufficient data")

    # 1) Higher-timeframe trend: EMA50 slope + Supertrend direction + ADX > 20.
    hc = htf["Close"]
    hes = ind.ema(hc, 50)
    hst = ind.supertrend(htf["High"], htf["Low"], hc, 10, 3.0)
    hadx = _f(ind.adx(htf["High"], htf["Low"], hc, 14)["adx"].iloc[-1])
    es_now, es_prev = _f(hes.iloc[-1]), _f(hes.iloc[-5])
    st_up = bool(hst["uptrend"].iloc[-1])
    strong = hadx is not None and hadx > 20 and es_now is not None and es_prev is not None
    htf_up = st_up and strong and es_now > es_prev
    htf_down = (not st_up) and strong and es_now < es_prev
    htf_state = "up" if htf_up else ("down" if htf_down else "flat")

    # 2) BTC regime (BTC at the higher timeframe) — alts follow BTC.
    bst = ind.supertrend(btc["High"], btc["Low"], btc["Close"], 10, 3.0)
    btc_up = bool(bst["uptrend"].iloc[-1])
    btc_state = "up" if btc_up else "risk-off"

    # 3) Lower-timeframe trigger around EMA21.
    close = df["Close"]
    e21 = ind.ema(close, 21)
    rsi_s = ind.rsi(close, 14)
    price = _f(close.iloc[-1])
    ema21 = _f(e21.iloc[-1])
    rsi_now, rsi_prev = _f(rsi_s.iloc[-1]), _f(rsi_s.iloc[-2])
    atrv = _f(ind.atr(df["High"], df["Low"], close, 14).iloc[-1])
    recent_low = _f(df["Low"].iloc[-5:-1].min())
    recent_high = _f(df["High"].iloc[-5:-1].max())
    if None in (price, ema21, rsi_now, rsi_prev, atrv, recent_low, recent_high):
        return make_signal("flat", reason="insufficient data")
    # long: pulled back to EMA21 then reclaimed with RSI recovering above 50.
    long_trigger = recent_low <= ema21 * 1.001 and price > ema21 and rsi_now > 50 and rsi_now >= rsi_prev
    # short: bounced up to EMA21 then rejected with RSI rolling below 50.
    short_trigger = recent_high >= ema21 * 0.999 and price < ema21 and rsi_now < 50 and rsi_now <= rsi_prev

    base_ind = {"htf_trend": htf_state, "btc_regime": btc_state, "ema21": round_price(ema21),
                "rsi": round(rsi_now, 1), "adx_htf": round(hadx, 1) if hadx else None}
    tf = ctx.get("timeframe", "")
    if htf_up and btc_up and long_trigger:
        stop = min(ema21, price - atr_mult * atrv)
        return make_signal("long", price, stop, price + rr * (price - stop),
                           reason=f"HTF trend up + BTC up + {tf} pullback reclaim", indicators=base_ind)
    if htf_down and not btc_up and short_trigger:
        stop = max(ema21, price + atr_mult * atrv)
        return make_signal("short", price, stop, price - rr * (stop - price),
                           reason=f"HTF trend down + BTC risk-off + {tf} failed bounce", indicators=base_ind)
    if htf_up and btc_up:
        why = "no pullback trigger"
    elif htf_down and not btc_up:
        why = "no failed-bounce trigger"
    else:
        why = "HTF/BTC not aligned"
    return make_signal("flat", reason=why, indicators=base_ind)


# ── Registration ────────────────────────────────────────────────────

register(BotDef("breakout", "Breakout", "signal",
                "Donchian break on expanding volume — long the breakout, short the breakdown.",
                "15m", TFS, evaluate=_breakout))
register(BotDef("mean-reversion", "Mean Reversion", "signal",
                "RSI(2) + Bollinger extreme — long oversold, short overbought (ranging markets).",
                "15m", TFS, evaluate=_mean_reversion))
register(BotDef("ema-trend", "EMA Trend", "signal",
                "EMA9 vs EMA21 confirmed by ADX + DI — ride established intraday trends either way.",
                "15m", TFS, evaluate=_ema_trend))
register(BotDef("vwap-reclaim", "VWAP Reclaim / Breakdown", "signal",
                "Reclaim the rolling VWAP from below (long) or lose it from above (short), with momentum.",
                "15m", TFS, evaluate=_vwap_reclaim))
register(BotDef("mtf-confluence", "Multi-Timeframe Confluence ★", "signal",
                "Trade only with the higher-TF trend AND BTC regime: long the pullback in an uptrend, "
                "short the failed bounce in a downtrend — trade the big trend, enter on the small one.",
                "15m", TFS, needs_htf=True, needs_btc=True, evaluate=_mtf_confluence))
