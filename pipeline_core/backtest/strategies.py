"""
Backtest strategy library — 35 price/volume-driven strategies.

Every strategy is long-only and fully computable from OHLCV history (the
only data honestly backtestable 10 years back with our sources — see
ENHANCEMENTS.md §2.1). A strategy declares:

  entry(df)  -> boolean Series (True on the signal day; execution is next open)
  exit(df)   -> boolean Series (checked daily while in position)
  + optional stop loss / trailing stop / profit target / time stop

Signals that reuse an indicator are written as named functions so each
indicator is computed once per stock, not once per expression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from pipeline_core.agents import indicators as ind

SignalFn = Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class StrategyDef:
    id: str
    name: str
    category: str
    description: str
    entry: SignalFn
    exit: SignalFn
    max_holding_days: Optional[int] = None
    stop_loss_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    target_pct: Optional[float] = None
    min_bars: int = 260  # history needed before signals are trusted
    direction: str = "long"  # "long" (default) | "short" — a short profits when price falls
                             # (stop above entry, target below); the engine mirrors every comparison.


def _crosses_above(a: pd.Series, b: pd.Series) -> pd.Series:
    """True on the bar where a crosses from ≤ b to > b."""
    return (a > b) & (a.shift(1) <= b.shift(1))


def _crosses_below(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def _fill(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool)


def _never(df: pd.DataFrame) -> pd.Series:
    """Exit signal for stop/target/time-only strategies."""
    return pd.Series(False, index=df.index)


def _rsi(close, n=14):
    return ind.rsi(close, n)


def _vol_ratio(df, n=20):
    return df["Volume"] / df["Volume"].rolling(n, min_periods=n).mean()


# ── Named multi-indicator signals ──────────────────────────────────


def _stoch_oversold_entry(df: pd.DataFrame) -> pd.Series:
    s = ind.stochastic(df["High"], df["Low"], df["Close"], 14, 3)
    return _fill(_crosses_above(s["k"], s["d"]) & (s["k"] < 20))


def _stoch_overbought_exit(df: pd.DataFrame) -> pd.Series:
    s = ind.stochastic(df["High"], df["Low"], df["Close"], 14, 3)
    return _fill(s["k"] > 80)


def _bb_lower_entry(df: pd.DataFrame) -> pd.Series:
    bb = ind.bollinger_bands(df["Close"], 20, 2.0)
    return _fill(_crosses_above(df["Close"], bb["lower"]))


def _bb_mid_exit(df: pd.DataFrame) -> pd.Series:
    bb = ind.bollinger_bands(df["Close"], 20, 2.0)
    return _fill(df["Close"] >= bb["mid"])


def _bb_below_mid_exit(df: pd.DataFrame) -> pd.Series:
    bb = ind.bollinger_bands(df["Close"], 20, 2.0)
    return _fill(df["Close"] < bb["mid"])


def _golden_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_above(ind.sma(c, 50), ind.sma(c, 200)))


def _golden_exit(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_below(ind.sma(c, 50), ind.sma(c, 200)))


def _ma2050_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_above(ind.sma(c, 20), ind.sma(c, 50)))


def _ma2050_exit(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_below(ind.sma(c, 20), ind.sma(c, 50)))


def _dma200_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    dma = ind.sma(c, 200)
    return _fill(_crosses_above(c, dma) & (dma.diff(5) > 0))


def _dma200_exit(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_below(c, ind.sma(c, 200)))


def _pullback50_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_above(c, ind.sma(c, 50)) & (c > ind.sma(c, 200)))


def _below_200dma_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(df["Close"] < ind.sma(df["Close"], 200))


def _macd_cross_entry(df: pd.DataFrame) -> pd.Series:
    m = ind.macd(df["Close"])
    return _fill(_crosses_above(m["macd"], m["signal"]) & (df["Close"] > ind.sma(df["Close"], 200)))


def _macd_cross_exit(df: pd.DataFrame) -> pd.Series:
    m = ind.macd(df["Close"])
    return _fill(_crosses_below(m["macd"], m["signal"]))


def _macd_hist_entry(df: pd.DataFrame) -> pd.Series:
    h = ind.macd(df["Close"])["histogram"]
    return _fill((h > 0) & (h.shift(1) <= 0) & (h.shift(5) < 0))


def _macd_hist_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(ind.macd(df["Close"])["histogram"] < 0)


def _rsi_thrust_entry(df: pd.DataFrame) -> pd.Series:
    r = _rsi(df["Close"])
    return _fill(_crosses_above(r, pd.Series(60.0, index=df.index)))


def _bb_upper_vol_entry(df: pd.DataFrame) -> pd.Series:
    bb = ind.bollinger_bands(df["Close"], 20, 2.0)
    return _fill((df["Close"] > bb["upper"]) & (_vol_ratio(df) > 1.5))


def _squeeze_entry(df: pd.DataFrame) -> pd.Series:
    bb = ind.bollinger_bands(df["Close"], 20, 2.0)
    squeeze = bb["width"] <= bb["width"].rolling(126, min_periods=126).quantile(0.10)
    return _fill(squeeze & (df["Close"] > bb["upper"]))


def _high52_entry(df: pd.DataFrame) -> pd.Series:
    return _fill((df["Close"] >= ind.rolling_high(df["High"], 252)) & (_vol_ratio(df) > 1.5))


def _low52_reversal_entry(df: pd.DataFrame) -> pd.Series:
    low_52 = ind.rolling_low(df["Low"], 252, skip_last=0)
    return _fill((df["Close"] <= low_52 * 1.05) & (_rsi(df["Close"]) < 35))


def _three_down_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    three_down = (c.diff() < 0) & (c.diff(2) < 0) & (c.diff(3) < 0)
    return _fill(three_down & (c.diff(4) >= 0) & (_rsi(c) < 40))


def _vol_surge_entry(df: pd.DataFrame) -> pd.Series:
    return _fill((df["Close"].pct_change() > 0.03) & (_vol_ratio(df) > 2.0))


def _gap_up_entry(df: pd.DataFrame) -> pd.Series:
    return _fill(
        (df["Open"] / df["Close"].shift(1) - 1 > 0.02)
        & (_vol_ratio(df) > 1.5)
        & (df["Close"] >= df["Open"])
    )


# ── Expansion signals (EMA / Supertrend / ADX / CCI / %R / OBV …) ───


def _ema_cross_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_above(ind.ema(c, 9), ind.ema(c, 21)))


def _ema_cross_exit(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill(_crosses_below(ind.ema(c, 9), ind.ema(c, 21)))


def _triple_ema_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    e8, e21, e55 = ind.ema(c, 8), ind.ema(c, 21), ind.ema(c, 55)
    return _fill(_crosses_above(c, e8) & (e8 > e21) & (e21 > e55))


def _triple_ema_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(df["Close"] < ind.ema(df["Close"], 21))


def _supertrend_flip_entry(df: pd.DataFrame) -> pd.Series:
    up = ind.supertrend(df["High"], df["Low"], df["Close"], 10, 3.0)["uptrend"]
    return _fill(up & ~up.shift(1).fillna(False).astype(bool))


def _supertrend_flip_exit(df: pd.DataFrame) -> pd.Series:
    up = ind.supertrend(df["High"], df["Low"], df["Close"], 10, 3.0)["uptrend"]
    return _fill(~up & up.shift(1).fillna(False).astype(bool))


def _adx_entry(df: pd.DataFrame) -> pd.Series:
    a = ind.adx(df["High"], df["Low"], df["Close"], 14)
    return _fill(_crosses_above(a["plus_di"], a["minus_di"]) & (a["adx"] > 25))


def _adx_exit(df: pd.DataFrame) -> pd.Series:
    a = ind.adx(df["High"], df["Low"], df["Close"], 14)
    return _fill(_crosses_below(a["plus_di"], a["minus_di"]))


def _roc_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    r = ind.roc(c, 63)
    return _fill(_crosses_above(r, pd.Series(12.0, index=df.index)) & (c > ind.sma(c, 200)))


def _roc_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(ind.roc(df["Close"], 63) < 0)


def _keltner_entry(df: pd.DataFrame) -> pd.Series:
    kc = ind.keltner_channels(df["High"], df["Low"], df["Close"], 20, 20, 2.0)
    c = df["Close"]
    return _fill((c > kc["upper"]) & (c > ind.sma(c, 200)))


def _keltner_exit(df: pd.DataFrame) -> pd.Series:
    kc = ind.keltner_channels(df["High"], df["Low"], df["Close"], 20, 20, 2.0)
    return _fill(df["Close"] < kc["mid"])


def _obv_entry(df: pd.DataFrame) -> pd.Series:
    o = ind.obv(df["Close"], df["Volume"])
    return _fill((o > ind.rolling_high(o, 50)) & (df["Close"] > ind.sma(df["Close"], 50)))


def _obv_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(df["Close"] < ind.sma(df["Close"], 50))


def _cci_entry(df: pd.DataFrame) -> pd.Series:
    c = ind.cci(df["High"], df["Low"], df["Close"], 20)
    return _fill(_crosses_above(c, pd.Series(-100.0, index=df.index)))


def _cci_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(ind.cci(df["High"], df["Low"], df["Close"], 20) > 100)


def _williams_entry(df: pd.DataFrame) -> pd.Series:
    wr = ind.williams_r(df["High"], df["Low"], df["Close"], 14)
    return _fill(_crosses_above(wr, pd.Series(-80.0, index=df.index)))


def _williams_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(ind.williams_r(df["High"], df["Low"], df["Close"], 14) > -30)


def _percent_b(df: pd.DataFrame) -> pd.Series:
    bb = ind.bollinger_bands(df["Close"], 20, 2.0)
    span = (bb["upper"] - bb["lower"]).replace(0, float("nan"))
    return (df["Close"] - bb["lower"]) / span


def _percent_b_entry(df: pd.DataFrame) -> pd.Series:
    return _fill((_percent_b(df) < 0.05) & (_rsi(df["Close"]) < 40))


def _percent_b_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(_percent_b(df) >= 0.5)


def _rsi2_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    return _fill((_rsi(c, 2) < 10) & (c > ind.sma(c, 200)))


def _rsi2_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(_rsi(df["Close"], 2) > 65)


def _gap_down_entry(df: pd.DataFrame) -> pd.Series:
    gap = df["Open"] / df["Close"].shift(1) - 1
    return _fill((gap < -0.03) & (df["Close"] > df["Open"]) & (_rsi(df["Close"]) < 45))


def _nr7_entry(df: pd.DataFrame) -> pd.Series:
    rng = df["High"] - df["Low"]
    nr7 = rng <= rng.rolling(7, min_periods=7).min()
    return _fill(nr7.shift(1).fillna(False).astype(bool) & (df["Close"] > df["High"].shift(1)))


def _inside_bar_entry(df: pd.DataFrame) -> pd.Series:
    inside = (df["High"].shift(1) < df["High"].shift(2)) & (df["Low"].shift(1) > df["Low"].shift(2))
    return _fill(inside & (df["Close"] > df["High"].shift(1)))


def _vcp_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    atr_pct = ind.atr(df["High"], df["Low"], c, 14) / c
    contraction = atr_pct <= atr_pct.rolling(126, min_periods=126).quantile(0.25)
    return _fill((c > ind.rolling_high(df["High"], 50)) & contraction & (c > ind.sma(c, 50)))


# ── Institutional accumulation signals (A/D line / CMF / MFI) ───────
# The honest, backtestable read on "institutional buying": raw 13F / FII-DII flows
# have no usable history (see ENHANCEMENTS.md §2.1), but the *footprint* big buyers
# leave in the tape — closes pinned near the highs on heavy volume, week after week —
# is fully in the OHLCV. These money-flow indicators quantify that accumulation.


def _inst_accum_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    money = ind.cmf(df["High"], df["Low"], c, df["Volume"], 21)
    adl = ind.ad_line(df["High"], df["Low"], c, df["Volume"])
    sma50 = ind.sma(c, 50)
    return _fill(
        (money > 0.10)                          # strong, sustained net buying pressure (weeks)
        & (adl > ind.rolling_high(adl, 50))     # A/D at a fresh 50-day high — accumulation confirmed
        & (c > sma50)                           # price participating, not diverging
        & (sma50.diff(5) > 0)                   # 50-DMA rising
        & (c > ind.sma(c, 200))                 # only in a primary uptrend — cuts falling-knife trades
    )


def _inst_accum_exit(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    money = ind.cmf(df["High"], df["Low"], c, df["Volume"], 21)
    return _fill((money < 0) | (c < ind.sma(c, 50)))


def _accum_breakout_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    adl = ind.ad_line(df["High"], df["Low"], c, df["Volume"])
    return _fill(
        (c > ind.rolling_high(df["High"], 20))  # 20-day price breakout …
        & (adl > ind.rolling_high(adl, 20))     # … that the A/D line confirms (not a low-conviction pop)
        & (_vol_ratio(df) > 1.5)                # on visibly heavier volume (size showing up)
        & (c > ind.sma(c, 200))                 # only in a primary uptrend
    )


def _mfi_thrust_entry(df: pd.DataFrame) -> pd.Series:
    c = df["Close"]
    money = ind.mfi(df["High"], df["Low"], c, df["Volume"], 14)
    return _fill(_crosses_above(money, pd.Series(50.0, index=df.index)) & (c > ind.sma(c, 200)))


def _mfi_thrust_exit(df: pd.DataFrame) -> pd.Series:
    return _fill(ind.mfi(df["High"], df["Low"], df["Close"], df["Volume"], 14) < 40)


# ── SEC 13F institutional accumulation (US only) ────────────────────
# Reads REAL quarterly institutional holdings that runner.py merges onto the daily
# frame (columns inst13f_shares / inst13f_qoq, forward-filled from each filing date so
# there's no lookahead) for US backtests. On any other market — or a name no tracked
# manager holds — the columns are absent and the strategy is simply inert.

def _has_13f(df: pd.DataFrame) -> bool:
    return "inst13f_qoq" in df.columns and "inst13f_shares" in df.columns


def _inst13f_fresh(df: pd.DataFrame) -> pd.Series:
    """True on the day a new 13F filing's data first posts (aggregate share count steps)."""
    shares = df["inst13f_shares"]
    return shares.ne(shares.shift(1)) & shares.notna()


def _inst13f_entry(df: pd.DataFrame) -> pd.Series:
    if not _has_13f(df):
        return _never(df)
    c = df["Close"]
    return _fill(_inst13f_fresh(df) & (df["inst13f_qoq"] >= 5.0) & (c > ind.sma(c, 200)))


def _inst13f_exit(df: pd.DataFrame) -> pd.Series:
    if not _has_13f(df):
        return _never(df)
    c = df["Close"]
    trimmed = _inst13f_fresh(df) & (df["inst13f_qoq"] < 0)
    return _fill(trimmed | (c < ind.sma(c, 200)))


# ── CFTC COT commercials accumulation (metals only) ─────────────────
# Reads the weekly COT index that runner.py merges onto the daily frame (column `cot_index`,
# forward-filled from each report's release date so there's no lookahead). "Follow the commercials":
# go long when the informed hedgers move into the top of their multi-year net-position range with
# price trending up. On any other market — or a symbol without COT data — the column is absent and
# the strategy is simply inert. See pipeline_core/services/cot.py.

def _has_cot(df: pd.DataFrame) -> bool:
    return "cot_index" in df.columns


def _cot_entry(df: pd.DataFrame) -> pd.Series:
    if not _has_cot(df):
        return _never(df)
    c, idx = df["Close"], df["cot_index"]
    # COT is a CONTRARIAN signal — commercials accumulate into weakness, so their net-long extreme is
    # a SETUP (in the top quartile of their 3y range), not a same-day trend confirmation. The TRIGGER
    # is price turning up (reclaiming the 20-DMA) while that setup is active — the informed money is
    # positioned and price confirms the turn. (Demanding price>50-DMA at the extreme itself fires ~never.)
    setup = idx >= 75
    trigger = _crosses_above(c, ind.sma(c, 20))
    return _fill(setup & trigger)


def _cot_exit(df: pd.DataFrame) -> pd.Series:
    if not _has_cot(df):
        return _never(df)
    c, idx = df["Close"], df["cot_index"]
    # Leave when the commercial edge fades (COT back to mid-range) or the trend breaks (loses 50-DMA).
    return _fill((idx < 50) | _crosses_below(c, ind.sma(c, 50)))


# ── US congressional-trades follow (US only) ────────────────────────
# Reads `congress_net` that runner.py merges from House Clerk PTR disclosures (a trailing net buy−sell
# dollar flow, dated by the PUBLIC disclosure date so there's no lookahead) — net positive means members
# of Congress have, on balance, been BUYING the name lately. Coattail it with a price-trend confirmation.
# Absent on non-US markets, names no member traded, or when the panel isn't built ⇒ inert.
# See pipeline_core/services/us_congress.py.

def _has_congress(df: pd.DataFrame) -> bool:
    return "congress_net" in df.columns


def _congress_entry(df: pd.DataFrame) -> pd.Series:
    if not _has_congress(df):
        return _never(df)
    c, net = df["Close"], df["congress_net"]
    # Setup: net congressional buying just turned positive (fresh disclosed accumulation). Trigger: price
    # confirms the trend (above its 50-DMA). Disclosure-lagged data, so the trend filter avoids chasing.
    setup = _crosses_above(net.fillna(0.0), 0.0)
    return _fill(setup & (c > ind.sma(c, 50)))


def _congress_exit(df: pd.DataFrame) -> pd.Series:
    if not _has_congress(df):
        return _never(df)
    c, net = df["Close"], df["congress_net"]
    # Leave when the disclosed buying flips to net selling or the trend breaks (loses the 50-DMA).
    return _fill((net.fillna(0.0) < 0) | _crosses_below(c, ind.sma(c, 50)))


# ── NSE bulk-deal follow (India only) ───────────────────────────────
# Reads `bulk_net_qty` that runner.py merges from disclosed NSE bulk/block deals (dated next-day,
# no lookahead) — a fresh net BUY means a large trader stepped in. Confirm with price and ride it.
# Absent on non-IN markets (or when NSE blocks the fetch) ⇒ inert. See services/in_smartmoney.py.

def _has_bulk(df: pd.DataFrame) -> bool:
    return "bulk_net_qty" in df.columns


def _bulk_entry(df: pd.DataFrame) -> pd.Series:
    if not _has_bulk(df):
        return _never(df)
    c, q = df["Close"], df["bulk_net_qty"]
    return _fill((q > 0) & (c > ind.sma(c, 20)))


def _bulk_exit(df: pd.DataFrame) -> pd.Series:
    if not _has_bulk(df):
        return _never(df)
    c = df["Close"]
    return _fill((df["bulk_net_qty"] < 0) | _crosses_below(c, ind.sma(c, 50)))


# ── Strategy definitions ───────────────────────────────────────────

STRATEGIES: list[StrategyDef] = [
    # ═══ Mean reversion ═══
    StrategyDef(
        id="rsi-oversold-reversal",
        name="RSI Oversold Reversal",
        category="Mean Reversion",
        description="Buy when RSI(14) drops below 30; exit when it recovers above 55 (or after 20 days).",
        entry=lambda df: _fill(_rsi(df["Close"]) < 30),
        exit=lambda df: _fill(_rsi(df["Close"]) > 55),
        max_holding_days=20,
    ),
    StrategyDef(
        id="rsi-extreme-oversold",
        name="RSI Extreme Oversold",
        category="Mean Reversion",
        description="Buy deep washouts (RSI < 25); exit at RSI 50 or 15 days.",
        entry=lambda df: _fill(_rsi(df["Close"]) < 25),
        exit=lambda df: _fill(_rsi(df["Close"]) > 50),
        max_holding_days=15,
    ),
    StrategyDef(
        id="stochastic-oversold-cross",
        name="Stochastic Oversold Cross",
        category="Mean Reversion",
        description="Buy when %K crosses above %D below 20; exit when %K reaches 80 (or 15 days).",
        entry=_stoch_oversold_entry,
        exit=_stoch_overbought_exit,
        max_holding_days=15,
    ),
    StrategyDef(
        id="bollinger-lower-reversal",
        name="Bollinger Lower Reversal",
        category="Mean Reversion",
        description="Buy when price closes back above the lower Bollinger Band; exit at the middle band (or 15 days).",
        entry=_bb_lower_entry,
        exit=_bb_mid_exit,
        max_holding_days=15,
    ),
    StrategyDef(
        id="three-down-days",
        name="Three Down Days Reversal",
        category="Mean Reversion",
        description="Buy after 3 consecutive down closes with RSI < 40; exit at +6% target or 12 days.",
        entry=_three_down_entry,
        exit=_never,
        target_pct=0.06,
        max_holding_days=12,
    ),
    StrategyDef(
        id="fifty-two-week-low-reversal",
        name="52-Week Low Reversal",
        category="Contrarian",
        description="Buy within 5% of the 52-week low with RSI < 35; exit at RSI 50 or 25 days.",
        entry=_low52_reversal_entry,
        exit=lambda df: _fill(_rsi(df["Close"]) > 50),
        max_holding_days=25,
    ),

    # ═══ Trend following ═══
    StrategyDef(
        id="golden-cross",
        name="Golden Cross (50/200 DMA)",
        category="Trend",
        description="Buy when the 50-DMA crosses above the 200-DMA; exit on the cross back down.",
        entry=_golden_entry,
        exit=_golden_exit,
    ),
    StrategyDef(
        id="ma-20-50-cross",
        name="20/50 DMA Cross",
        category="Trend",
        description="Faster trend cross: 20-DMA above 50-DMA entry, cross below exit.",
        entry=_ma2050_entry,
        exit=_ma2050_exit,
    ),
    StrategyDef(
        id="dma200-regime",
        name="200-DMA Regime",
        category="Trend",
        description="Buy when price crosses above a *rising* 200-DMA; exit on the cross below.",
        entry=_dma200_entry,
        exit=_dma200_exit,
    ),
    StrategyDef(
        id="trend-pullback-50dma",
        name="Trend Pullback to 50-DMA",
        category="Trend",
        description="In an uptrend (above 200-DMA), buy the bounce back above the 50-DMA; exit if the 200-DMA breaks (or 30 days).",
        entry=_pullback50_entry,
        exit=_below_200dma_exit,
        max_holding_days=30,
    ),

    # ═══ Momentum ═══
    StrategyDef(
        id="rsi-momentum-thrust",
        name="RSI Momentum Thrust",
        category="Momentum",
        description="Buy when RSI crosses up through 60 (strength emerging); exit below 45 (or 20 days).",
        entry=_rsi_thrust_entry,
        exit=lambda df: _fill(_rsi(df["Close"]) < 45),
        max_holding_days=20,
    ),
    StrategyDef(
        id="macd-bull-cross-200dma",
        name="MACD Bull Cross in Uptrend",
        category="Momentum",
        description="Buy MACD/signal bullish crosses only above the 200-DMA; exit on the bear cross.",
        entry=_macd_cross_entry,
        exit=_macd_cross_exit,
    ),
    StrategyDef(
        id="macd-histogram-reversal",
        name="MACD Histogram Reversal",
        category="Momentum",
        description="Buy when the MACD histogram turns positive after a negative streak; exit when it flips back.",
        entry=_macd_hist_entry,
        exit=_macd_hist_exit,
        max_holding_days=25,
    ),
    StrategyDef(
        id="volume-surge-thrust",
        name="Volume Surge Thrust",
        category="Momentum",
        description="Buy +3% days on 2× average volume; exit with an 8% trailing stop or 10 days.",
        entry=_vol_surge_entry,
        exit=_never,
        trailing_stop_pct=0.08,
        max_holding_days=10,
    ),
    StrategyDef(
        id="gap-up-continuation",
        name="Gap-Up Continuation",
        category="Momentum",
        description="Buy >2% gap-ups with volume that hold above the open; 5% trailing stop, 5-day max hold.",
        entry=_gap_up_entry,
        exit=_never,
        trailing_stop_pct=0.05,
        max_holding_days=5,
    ),

    # ═══ Breakout ═══
    StrategyDef(
        id="fifty-two-week-high-breakout",
        name="52-Week High Breakout",
        category="Breakout",
        description="Buy new 52-week highs on 1.5× volume; 10% trailing stop or 20 days.",
        entry=_high52_entry,
        exit=_never,
        trailing_stop_pct=0.10,
        max_holding_days=20,
    ),
    StrategyDef(
        id="donchian-20-breakout",
        name="Donchian 20 Breakout",
        category="Breakout",
        description="Buy closes above the prior 20-day high; exit on a close below the prior 10-day low.",
        entry=lambda df: _fill(df["Close"] > ind.rolling_high(df["High"], 20)),
        exit=lambda df: _fill(df["Close"] < ind.rolling_low(df["Low"], 10)),
        min_bars=60,
    ),
    StrategyDef(
        id="donchian-55-turtle",
        name="Donchian 55 (Turtle)",
        category="Breakout",
        description="Classic turtle rule: buy 55-day highs, exit on 20-day lows.",
        entry=lambda df: _fill(df["Close"] > ind.rolling_high(df["High"], 55)),
        exit=lambda df: _fill(df["Close"] < ind.rolling_low(df["Low"], 20)),
        min_bars=80,
    ),
    StrategyDef(
        id="bollinger-upper-breakout",
        name="Bollinger Upper Breakout",
        category="Breakout",
        description="Buy closes above the upper band with volume confirmation; exit at the middle band.",
        entry=_bb_upper_vol_entry,
        exit=_bb_below_mid_exit,
        max_holding_days=20,
    ),
    StrategyDef(
        id="volatility-squeeze-breakout",
        name="Volatility Squeeze Breakout",
        category="Breakout",
        description="Buy when a 6-month band-width squeeze (lowest 10%) fires upward through the upper band; exit at the middle band.",
        entry=_squeeze_entry,
        exit=_bb_below_mid_exit,
        max_holding_days=20,
        min_bars=160,
    ),

    # ═══ Trend (expansion) ═══
    StrategyDef(
        id="ema-9-21-cross",
        name="EMA 9/21 Crossover",
        category="Trend",
        description="Fast exponential trend: buy when the 9-EMA crosses above the 21-EMA; exit on the cross back below.",
        entry=_ema_cross_entry,
        exit=_ema_cross_exit,
        min_bars=60,
    ),
    StrategyDef(
        id="triple-ema-alignment",
        name="Triple EMA Alignment (8/21/55)",
        category="Trend",
        description="Buy when price reclaims the 8-EMA while 8>21>55 EMAs are stacked bullishly; exit when price loses the 21-EMA.",
        entry=_triple_ema_entry,
        exit=_triple_ema_exit,
        min_bars=120,
    ),
    StrategyDef(
        id="supertrend-flip",
        name="Supertrend Flip (10, 3)",
        category="Trend",
        description="Buy when Supertrend(10, 3×ATR) flips to an uptrend; exit when it flips back down.",
        entry=_supertrend_flip_entry,
        exit=_supertrend_flip_exit,
        min_bars=60,
    ),
    StrategyDef(
        id="adx-trend-rider",
        name="ADX Trend Rider",
        category="Trend",
        description="Buy when +DI crosses above −DI with ADX > 25 (strong trend building); exit when −DI retakes +DI.",
        entry=_adx_entry,
        exit=_adx_exit,
        max_holding_days=60,
        min_bars=60,
    ),

    # ═══ Momentum / Volume (expansion) ═══
    StrategyDef(
        id="roc-momentum",
        name="63-Day ROC Momentum",
        category="Momentum",
        description="Buy when quarterly rate-of-change crosses above +12% in an uptrend (above 200-DMA); exit when momentum turns negative (or 40 days).",
        entry=_roc_entry,
        exit=_roc_exit,
        max_holding_days=40,
        min_bars=260,
    ),
    StrategyDef(
        id="keltner-breakout",
        name="Keltner Channel Breakout",
        category="Momentum",
        description="Buy closes above the upper Keltner band (EMA20 + 2×ATR) in an uptrend; exit back at the mid EMA (or 25 days).",
        entry=_keltner_entry,
        exit=_keltner_exit,
        max_holding_days=25,
        min_bars=260,
    ),
    StrategyDef(
        id="obv-breakout",
        name="OBV 50-Day Breakout",
        category="Volume",
        description="Accumulation signal: buy when On-Balance Volume makes a new 50-day high with price above the 50-DMA; exit below the 50-DMA.",
        entry=_obv_entry,
        exit=_obv_exit,
        min_bars=90,
    ),

    # ═══ Mean reversion (expansion) ═══
    StrategyDef(
        id="cci-reversal",
        name="CCI Oversold Reversal",
        category="Mean Reversion",
        description="Buy when CCI(20) crosses back above −100 from oversold; exit when it reaches +100 (or 15 days).",
        entry=_cci_entry,
        exit=_cci_exit,
        max_holding_days=15,
        min_bars=60,
    ),
    StrategyDef(
        id="williams-r-oversold",
        name="Williams %R Rebound",
        category="Mean Reversion",
        description="Buy when Williams %R(14) crosses up out of oversold (−80); exit when it recovers past −30 (or 12 days).",
        entry=_williams_entry,
        exit=_williams_exit,
        max_holding_days=12,
        min_bars=60,
    ),
    StrategyDef(
        id="bollinger-percent-b",
        name="Bollinger %B Snapback",
        category="Mean Reversion",
        description="Buy washouts below the lower band (%B < 0.05) with RSI < 40; exit at the middle band (%B ≥ 0.5) or 12 days.",
        entry=_percent_b_entry,
        exit=_percent_b_exit,
        max_holding_days=12,
        min_bars=60,
    ),
    StrategyDef(
        id="connors-rsi2",
        name="Connors RSI(2)",
        category="Mean Reversion",
        description="Larry Connors' short-term dip buy: RSI(2) < 10 while above the 200-DMA; exit when RSI(2) > 65 (or 10 days).",
        entry=_rsi2_entry,
        exit=_rsi2_exit,
        max_holding_days=10,
        min_bars=260,
    ),
    StrategyDef(
        id="gap-down-reversal",
        name="Gap-Down Reversal",
        category="Mean Reversion",
        description="Buy >3% gap-downs that close green (buyers stepped in) with RSI < 45; exit at +6% target or 10 days.",
        entry=_gap_down_entry,
        exit=lambda df: _fill(_rsi(df["Close"]) > 55),
        target_pct=0.06,
        max_holding_days=10,
        min_bars=60,
    ),

    # ═══ Breakout (expansion) ═══
    StrategyDef(
        id="nr7-breakout",
        name="NR7 Range Breakout",
        category="Breakout",
        description="Buy the breakout above a narrowest-range-in-7-days bar (volatility coiling → expansion); 8% trailing stop or 10 days.",
        entry=_nr7_entry,
        exit=_never,
        trailing_stop_pct=0.08,
        max_holding_days=10,
        min_bars=40,
    ),
    StrategyDef(
        id="inside-bar-breakout",
        name="Inside Bar Breakout",
        category="Breakout",
        description="Buy when price breaks above an inside-bar (range compression) high; 8% trailing stop or 12 days.",
        entry=_inside_bar_entry,
        exit=_never,
        trailing_stop_pct=0.08,
        max_holding_days=12,
        min_bars=40,
    ),
    StrategyDef(
        id="volatility-contraction-breakout",
        name="Volatility Contraction Breakout",
        category="Breakout",
        description="Minervini-style VCP: buy a 50-day high breakout that fires while ATR% sits in its lowest quartile (tight base); 12% trailing stop or 40 days.",
        entry=_vcp_entry,
        exit=_never,
        trailing_stop_pct=0.12,
        max_holding_days=40,
        min_bars=160,
    ),

    # ═══ Institutional accumulation (money-flow footprint) ═══
    StrategyDef(
        id="institutional-accumulation",
        name="Institutional Accumulation",
        category="Accumulation",
        description="Track the footprint of institutional buying: strong Chaikin Money Flow (> 0.10) with "
                    "the Accumulation/Distribution line at a fresh 50-day high, price above a rising 50-DMA "
                    "and in a primary uptrend (200-DMA). Rides the trend — exits when money flow turns "
                    "negative or price loses the 50-DMA.",
        entry=_inst_accum_entry,
        exit=_inst_accum_exit,
        min_bars=260,
    ),
    StrategyDef(
        id="accumulation-breakout",
        name="Accumulation Breakout",
        category="Accumulation",
        description="A 20-day price breakout confirmed by the A/D line making its own new high on 1.5× "
                    "volume, in a primary uptrend (above 200-DMA) — big money stepping in at the breakout. "
                    "10% trailing stop or 30 days.",
        entry=_accum_breakout_entry,
        exit=_never,
        trailing_stop_pct=0.10,
        max_holding_days=30,
        min_bars=260,
    ),
    StrategyDef(
        id="money-flow-thrust",
        name="Money-Flow Thrust (MFI)",
        category="Accumulation",
        description="Buy when the volume-weighted Money Flow Index crosses up through 50 (money rotating in) "
                    "while price is above the 200-DMA; exit when the MFI falls back below 40 (or 25 days).",
        entry=_mfi_thrust_entry,
        exit=_mfi_thrust_exit,
        max_holding_days=25,
        min_bars=260,
    ),
    StrategyDef(
        id="institutional-13f-accumulation",
        name="Institutional Accumulation (SEC 13F)",
        category="Accumulation",
        description="US only. Buy when a basket of ~30 large institutional managers grew their "
                    "aggregate stake ≥5% in the latest quarterly 13F filing, price above the 200-DMA. "
                    "Rides the trend — exits when the managers trim (next filing turns negative) or "
                    "price loses the 200-DMA. Real filed holdings (quarterly, ~45-day lag), not a proxy.",
        entry=_inst13f_entry,
        exit=_inst13f_exit,
        min_bars=260,
    ),
    StrategyDef(
        id="cot-commercials-follow",
        name="COT Commercials Accumulation",
        category="Accumulation",
        description="Metals only. Follow the 'smart money' hedgers, who accumulate into weakness: when "
                    "CFTC COMMERCIALS sit in the top quartile of their 3-year net-position range (COT index "
                    "≥ 75), buy the confirmation — price reclaiming its 20-DMA; exit when the edge fades "
                    "(COT < 50) or the trend breaks (loses the 50-DMA). Real weekly CFTC positioning "
                    "(released Fri for Tue), forward-filled — not a proxy. Inert without COT data.",
        entry=_cot_entry,
        exit=_cot_exit,
        min_bars=260,
    ),
    StrategyDef(
        id="bulk-deal-follow",
        name="Bulk-Deal Accumulation (NSE)",
        category="Accumulation",
        description="India only. Coattail disclosed size: buy when a net BUY bulk/block deal is reported "
                    "for the stock (a large trader stepped in, SEBI-disclosed after the close) and price "
                    "confirms above the 20-DMA; exit on a net-sell disclosure, a 50-DMA break, or after "
                    "40 days. Real NSE bulk deals, dated next-day (no lookahead). Inert without the data.",
        entry=_bulk_entry,
        exit=_bulk_exit,
        max_holding_days=40,
        min_bars=260,
    ),
    StrategyDef(
        id="congress-buy-follow",
        name="Congressional Buying (House PTR)",
        category="Accumulation",
        description="US only. Coattail disclosed political-insider buying: when members of Congress have, "
                    "on balance, been BUYING a stock (net positive House PTR flow just turned up) and price "
                    "confirms above the 50-DMA, buy it; exit when the disclosed flow flips to net selling or "
                    "the trend breaks (loses the 50-DMA). Real House Clerk filings, dated by the public "
                    "disclosure date (~45-day reporting lag, no lookahead). Inert without the congress panel.",
        entry=_congress_entry,
        exit=_congress_exit,
        min_bars=260,
    ),
]

_BY_ID = {s.id: s for s in STRATEGIES}
CATEGORIES = sorted({s.category for s in STRATEGIES})


def get_strategy(strategy_id: str) -> StrategyDef:
    return _BY_ID[strategy_id]


# ── Crypto-native strategies (regime-gated trend / breakout, wide stops) ──────────
# Crypto used to run the full equity library unchanged, including 10 mean-reversion +
# contrarian strategies that catch falling knives in crypto bear markets (−90%+ backtested
# drawdowns) and dilute idea conviction with near-wipeout votes. For CRYPTO we instead run a
# curated TREND / MOMENTUM / BREAKOUT subset (the regime-aware half of the library) plus the
# crypto-tuned strategies below. Every long here is gated by a per-coin risk-on filter — price
# above a *rising* long MA, since there's no BTC series inside a single-coin entry fn — and
# carries a WIDE trailing stop, because crypto's ordinary noise shakes out equity-tight stops.


def _crypto_regime_ok(df: pd.DataFrame, n: int = 100) -> pd.Series:
    """Per-coin risk-on proxy: price above a *rising* long MA. The FALLBACK for the market-wide gate
    below when the injected BTC-regime column is absent (unit tests / non-crypto paths)."""
    c = df["Close"]
    ma = ind.sma(c, n)
    return (c > ma) & (ma > ma.shift(10))


# ── Market-wide regime gate ──────────────────────────────────────────────────────────────────────
# runner.run_full_backtest injects a boolean `MKT_RISK_ON` column (the market-wide BTC regime, from
# services/market_regime.enrich_frames_with_market) onto every crypto frame, so a single-coin entry/exit
# can respect the market-wide tide — crypto's dominant risk factor. When the column is ABSENT (unit tests,
# non-crypto, any path that didn't inject) both helpers fall back to the per-coin proxy, so the entries
# reduce EXACTLY to the prior per-coin behaviour. `_mkt_risk_off` references `_crypto_regime_off` (defined
# with the shorts below); module-level functions resolve at call time so the forward reference is fine.
def _mkt_risk_on(df: pd.DataFrame) -> pd.Series:
    """True where the market-wide benchmark (BTC) is risk-on; per-coin proxy when the column is absent."""
    if "MKT_RISK_ON" in df.columns:
        return df["MKT_RISK_ON"].astype(bool)
    return _crypto_regime_ok(df)


def _mkt_risk_off(df: pd.DataFrame) -> pd.Series:
    """True where the market-wide benchmark (BTC) is risk-off; per-coin proxy when the column is absent."""
    if "MKT_RISK_ON" in df.columns:
        return ~df["MKT_RISK_ON"].astype(bool)
    return _crypto_regime_off(df)


def _crypto_breakout_entry(df: pd.DataFrame) -> pd.Series:
    """Donchian 20-day high breakout, taken only when the coin AND the market (BTC) are risk-on."""
    c = df["Close"]
    return _fill((c > ind.rolling_high(c, 20)) & _crypto_regime_ok(df, 100) & _mkt_risk_on(df))


def _crypto_trend_exit(df: pd.DataFrame) -> pd.Series:
    """Structural exit: close crosses below the 50-DMA (trend broken)."""
    c = df["Close"]
    return _fill(_crosses_below(c, ind.sma(c, 50)))


def _crypto_regime_exit(df: pd.DataFrame) -> pd.Series:
    """Long exit: the structural 50-DMA break OR the market flipping risk-off — get to cash when BTC
    rolls over, which is what turns the ~−90% crypto-winter drawdowns into flat equity. Without the
    injected column the market term is a no-op, so this reduces to the plain 50-DMA exit."""
    base = _crypto_trend_exit(df)
    if "MKT_RISK_ON" in df.columns:
        base = base | (~df["MKT_RISK_ON"].astype(bool))
    return _fill(base)


def _crypto_momentum_entry(df: pd.DataFrame) -> pd.Series:
    """Strong 30-day momentum emerging (ROC crosses above +20%) with the coin AND market (BTC) risk-on."""
    c = df["Close"]
    return _fill(_crosses_above(ind.roc(c, 30), pd.Series(20.0, index=df.index))
                 & _crypto_regime_ok(df, 100) & _mkt_risk_on(df))


def _crypto_supertrend_entry(df: pd.DataFrame) -> pd.Series:
    """Supertrend bullish flip confirmed by the coin AND market (BTC) being risk-on."""
    up = ind.supertrend(df["High"], df["Low"], df["Close"], 10, 3.0)["uptrend"]
    flip = up & ~up.shift(1).fillna(False).astype(bool)
    return _fill(flip & _crypto_regime_ok(df, 100) & _mkt_risk_on(df))


def _crypto_supertrend_regime_exit(df: pd.DataFrame) -> pd.Series:
    """Long exit for the Supertrend strategy: the bearish flip OR the market flipping risk-off (no-op
    without the injected column, so it reduces to the plain flip exit)."""
    base = _supertrend_flip_exit(df)
    if "MKT_RISK_ON" in df.columns:
        base = base | (~df["MKT_RISK_ON"].astype(bool))
    return _fill(base)


def _crypto_regime_trend_entry(df: pd.DataFrame) -> pd.Series:
    """100-day Donchian high breakout, taken only while the market (BTC) is risk-on — trend-follow the
    strongest coins in a bull market and sit out the winter."""
    c = df["Close"]
    return _fill((c > ind.rolling_high(c, 100)) & _mkt_risk_on(df))


_CRYPTO_NATIVE: list[StrategyDef] = [
    StrategyDef(
        id="crypto-breakout-regime",
        name="Crypto Breakout (Regime-Gated)",
        category="Breakout",
        description="Crypto: buy a 20-day high breakout only while the coin AND the market (BTC) are "
                    "risk-on; ride with a wide 15% trailing stop, or exit on a 50-DMA break OR when BTC "
                    "flips risk-off. The market gate is what avoids buying alts into a crypto winter.",
        entry=_crypto_breakout_entry,
        exit=_crypto_regime_exit,
        trailing_stop_pct=0.15,
        max_holding_days=45,
        min_bars=140,
    ),
    StrategyDef(
        id="crypto-momentum-regime",
        name="Crypto Momentum (Regime-Gated)",
        category="Momentum",
        description="Crypto: buy when 30-day momentum crosses above +20% with the coin AND market (BTC) "
                    "risk-on; exit on a 50-DMA break, a BTC risk-off flip, or after 40 days. Wide 18% trail.",
        entry=_crypto_momentum_entry,
        exit=_crypto_regime_exit,
        trailing_stop_pct=0.18,
        max_holding_days=40,
        min_bars=140,
    ),
    StrategyDef(
        id="crypto-supertrend-regime",
        name="Crypto Supertrend (Regime-Gated)",
        category="Trend",
        description="Crypto: enter on a Supertrend bullish flip confirmed by the coin AND market (BTC) "
                    "being risk-on; exit on the bearish flip OR a BTC risk-off flip. Wide 20% trailing stop.",
        entry=_crypto_supertrend_entry,
        exit=_crypto_supertrend_regime_exit,
        trailing_stop_pct=0.20,
        min_bars=140,
    ),
    StrategyDef(
        id="crypto-regime-trend",
        name="Crypto Regime Trend (BTC-Gated)",
        category="Trend",
        description="Crypto: buy a 100-day high breakout ONLY while BTC is risk-on (above a rising "
                    "200-DMA), and get straight to cash the moment BTC flips risk-off. A pure "
                    "'trend-follow the bull, sit out the winter' book — built to cut the ~−90% crypto "
                    "drawdown rather than chase the biggest raw CAGR. Wide 20% trailing stop.",
        entry=_crypto_regime_trend_entry,
        exit=_crypto_regime_exit,
        trailing_stop_pct=0.20,
        min_bars=140,
    ),
]


# ── Crypto SHORT strategies (bearish mirrors of the crypto-native longs) ──────────
# The daily board is long-only for every equity/metal market, but crypto trades both ways: it trends
# down as cleanly as it trends up. These are the exact mirror of the three crypto-native longs above —
# a risk-OFF regime filter (price below a *falling* long MA), a bearish entry, and a structural
# reclaim exit — with the same wide trailing stops. direction="short", so the engine flips every
# comparison (stop above entry, target below) and inverts the P&L. Only produced for CRYPTO, and only
# when settings.crypto_shorts_enabled is on.


def _crypto_regime_off(df: pd.DataFrame, n: int = 100) -> pd.Series:
    """Per-coin risk-OFF proxy: price below a *falling* long MA — the mirror of `_crypto_regime_ok`.
    Stands in for a market-wide (BTC) bear filter, unavailable inside a single-coin entry signal."""
    c = df["Close"]
    ma = ind.sma(c, n)
    return (c < ma) & (ma < ma.shift(10))


def _crypto_breakdown_entry(df: pd.DataFrame) -> pd.Series:
    """Donchian 20-day low breakdown, taken only while the coin AND the market (BTC) are risk-off —
    shorting crypto only pays in a market-wide downtrend, never into a bull market."""
    c = df["Close"]
    return _fill((c < ind.rolling_low(c, 20)) & _crypto_regime_off(df, 100) & _mkt_risk_off(df))


def _crypto_trend_reclaim_exit(df: pd.DataFrame) -> pd.Series:
    """Structural cover: close crosses back above the 50-DMA (downtrend broken)."""
    c = df["Close"]
    return _fill(_crosses_above(c, ind.sma(c, 50)))


def _crypto_regime_cover_exit(df: pd.DataFrame) -> pd.Series:
    """Short cover: the structural 50-DMA reclaim OR the market flipping risk-on (get flat when BTC
    turns up). No-op without the injected column, so it reduces to the plain 50-DMA reclaim."""
    base = _crypto_trend_reclaim_exit(df)
    if "MKT_RISK_ON" in df.columns:
        base = base | df["MKT_RISK_ON"].astype(bool)
    return _fill(base)


def _crypto_momentum_down_entry(df: pd.DataFrame) -> pd.Series:
    """Strong 30-day downside momentum (ROC crosses below −20%) with the coin AND market (BTC) risk-off."""
    c = df["Close"]
    return _fill(_crosses_below(ind.roc(c, 30), pd.Series(-20.0, index=df.index))
                 & _crypto_regime_off(df, 100) & _mkt_risk_off(df))


def _crypto_supertrend_down_entry(df: pd.DataFrame) -> pd.Series:
    """Supertrend bearish flip confirmed by the coin AND market (BTC) being risk-off."""
    up = ind.supertrend(df["High"], df["Low"], df["Close"], 10, 3.0)["uptrend"]
    flip = ~up & up.shift(1).fillna(False).astype(bool)
    return _fill(flip & _crypto_regime_off(df, 100) & _mkt_risk_off(df))


def _crypto_supertrend_bull_exit(df: pd.DataFrame) -> pd.Series:
    """Cover a short on the Supertrend bullish flip (down → up)."""
    up = ind.supertrend(df["High"], df["Low"], df["Close"], 10, 3.0)["uptrend"]
    return _fill(up & ~up.shift(1).fillna(False).astype(bool))


def _crypto_supertrend_regime_cover_exit(df: pd.DataFrame) -> pd.Series:
    """Short cover for the Supertrend strategy: the bullish flip OR the market flipping risk-on (no-op
    without the injected column, so it reduces to the plain bullish-flip cover)."""
    base = _crypto_supertrend_bull_exit(df)
    if "MKT_RISK_ON" in df.columns:
        base = base | df["MKT_RISK_ON"].astype(bool)
    return _fill(base)


_CRYPTO_SHORT: list[StrategyDef] = [
    StrategyDef(
        id="crypto-breakdown-regime",
        name="Crypto Breakdown Short (Regime-Gated)",
        category="Breakout",
        description="Crypto SHORT: sell a 20-day low breakdown only while the coin AND the market (BTC) "
                    "are risk-off; cover on a 50-DMA reclaim, a BTC risk-on flip, or a 15% trailing stop "
                    "off the trough. The market-off gate stops it shorting into a bull market.",
        entry=_crypto_breakdown_entry,
        exit=_crypto_regime_cover_exit,
        trailing_stop_pct=0.15,
        max_holding_days=45,
        min_bars=140,
        direction="short",
    ),
    StrategyDef(
        id="crypto-momentum-down-regime",
        name="Crypto Momentum Short (Regime-Gated)",
        category="Momentum",
        description="Crypto SHORT: sell when 30-day momentum crosses below −20% with the coin AND market "
                    "(BTC) risk-off; cover on a 50-DMA reclaim, a BTC risk-on flip, or after 40 days. "
                    "Wide 18% trail off the trough.",
        entry=_crypto_momentum_down_entry,
        exit=_crypto_regime_cover_exit,
        trailing_stop_pct=0.18,
        max_holding_days=40,
        min_bars=140,
        direction="short",
    ),
    StrategyDef(
        id="crypto-supertrend-down-regime",
        name="Crypto Supertrend Short (Regime-Gated)",
        category="Trend",
        description="Crypto SHORT: enter on a Supertrend bearish flip confirmed by the coin AND market "
                    "(BTC) being risk-off; cover on the bullish flip or a BTC risk-on flip. Wide 20% stop.",
        entry=_crypto_supertrend_down_entry,
        exit=_crypto_supertrend_regime_cover_exit,
        trailing_stop_pct=0.20,
        min_bars=140,
        direction="short",
    ),
]


# The TREND / MOMENTUM / BREAKOUT / footprint-accumulation half of the library — regime-aware,
# momentum-persistent, what actually works in crypto. Daily mean-reversion / contrarian strategies
# and the market-specific accumulation strategies (13F / COT / bulk-deal) are excluded for crypto.
_CRYPTO_REUSE_IDS = [
    # Trend
    "golden-cross", "ma-20-50-cross", "dma200-regime", "trend-pullback-50dma",
    "ema-9-21-cross", "triple-ema-alignment", "supertrend-flip", "adx-trend-rider",
    # Momentum
    "rsi-momentum-thrust", "macd-bull-cross-200dma", "volume-surge-thrust",
    "gap-up-continuation", "roc-momentum", "keltner-breakout",
    # Breakout
    "fifty-two-week-high-breakout", "donchian-20-breakout", "donchian-55-turtle",
    "bollinger-upper-breakout", "volatility-squeeze-breakout", "nr7-breakout",
    "inside-bar-breakout", "volatility-contraction-breakout",
    # Volume / footprint accumulation (price/volume based, market-agnostic)
    "obv-breakout", "institutional-accumulation", "accumulation-breakout", "money-flow-thrust",
]

CRYPTO_STRATEGIES: list[StrategyDef] = [_BY_ID[i] for i in _CRYPTO_REUSE_IDS] + _CRYPTO_NATIVE

# Crypto SHORT strategies — surfaced only when settings.crypto_shorts_enabled (default on). Kept in a
# separate list so `strategies_for_market` can gate them, but always folded into ALL_STRATEGIES below
# so a persisted short signal resolves back to its def even if the flag is later turned off.
CRYPTO_SHORT_STRATEGIES: list[StrategyDef] = _CRYPTO_SHORT


STRATEGIES_BY_MARKET: dict[str, list[StrategyDef]] = {
    "CRYPTO": CRYPTO_STRATEGIES,
}


def strategies_for_market(market: str) -> list[StrategyDef]:
    """The backtest strategy set for a market. Most markets run the full equity+shared library
    (`STRATEGIES`); CRYPTO runs a curated trend/momentum/breakout set plus crypto-native
    strategies (`CRYPTO_STRATEGIES`), because the daily mean-reversion strategies catch falling
    knives in crypto bear markets. Crypto additionally runs a small bearish-mirror SHORT set
    (`CRYPTO_SHORT_STRATEGIES`) when settings.crypto_shorts_enabled is on, so the board trades both
    ways. Definitions are shared objects — safe to read, never mutate."""
    mk = (market or "IN").upper()
    if mk == "CRYPTO":
        from pipeline_core.config import settings          # lazy — avoid an import cycle
        shorts = CRYPTO_SHORT_STRATEGIES if settings.crypto_shorts_enabled else []
        return CRYPTO_STRATEGIES + shorts
    return STRATEGIES_BY_MARKET.get(mk, STRATEGIES)


# Union of every market's strategies, de-duplicated by id — the global lookup used to resolve a
# persisted signal's id back to its StrategyDef regardless of which market produced it. Crypto shorts
# are always included here (independent of the flag) so their persisted signals stay resolvable.
ALL_STRATEGIES: list[StrategyDef] = list(
    {s.id: s for lst in [STRATEGIES, *STRATEGIES_BY_MARKET.values(), CRYPTO_SHORT_STRATEGIES]
     for s in lst}.values()
)
