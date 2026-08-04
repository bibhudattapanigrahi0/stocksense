"""
Technical indicators — pure pandas/numpy implementations.

Replaces the pandas-ta dependency: RSI (Wilder), SMA, EMA, and MACD are
small, well-defined, and far easier to unit-test when owned here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, length: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average (adjust=False matches TA convention)."""
    return series.ewm(span=length, adjust=False).mean()


def _rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's moving average (RMA) with an SMA seed — matches the canonical
    RSI used by TradingView/StockCharts (ta.rma), unlike a plain
    ewm(alpha=1/n) which seeds from the first observation and needs ~2n
    extra points to converge.
    """
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = ~np.isnan(values)
    if valid.sum() < length:
        return pd.Series(out, index=series.index)

    idx = np.flatnonzero(valid)
    seed_pos = idx[length - 1]
    out[seed_pos] = float(np.mean(values[idx[0] : seed_pos + 1]))
    alpha = 1.0 / length
    for i in range(seed_pos + 1, len(values)):
        x = values[i]
        out[i] = out[i - 1] if np.isnan(x) else alpha * x + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """
    Relative Strength Index using Wilder's smoothing (RMA, SMA-seeded),
    matching the values published by charting platforms.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length)

    rs = avg_gain / avg_loss
    result = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 → RSI is 100 by definition (all gains over the window)
    result = result.where(avg_loss != 0, 100.0)
    return result


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """
    MACD line, histogram, and signal line.
    Returns a DataFrame with columns: macd, histogram, signal.
    """
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "histogram": histogram, "signal": signal_line})


def annualized_volatility_pct(close: pd.Series, trading_days: int = 252) -> float:
    """Annualized volatility of daily log-simple returns, in percent."""
    returns = close.pct_change().dropna()
    if returns.empty:
        return float("nan")
    return float(returns.std() * np.sqrt(trading_days) * 100)


def beta_vs_benchmark(close: pd.Series, benchmark_close: pd.Series, trading_days: int = 252) -> float | None:
    """
    Beta of a stock against a benchmark index using aligned daily returns.
    Returns None when there isn't enough overlapping data.
    """
    stock_ret = close.pct_change()
    bench_ret = benchmark_close.pct_change()
    aligned = pd.concat([stock_ret, bench_ret], axis=1, keys=["s", "b"]).dropna()
    if len(aligned) < 30:
        return None
    cov = aligned["s"].cov(aligned["b"])
    var = aligned["b"].var()
    if var == 0 or np.isnan(var):
        return None
    return float(cov / var)


def bollinger_bands(close: pd.Series, length: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: middle (SMA), upper, lower, and bandwidth (relative)."""
    mid = sma(close, length)
    std = close.rolling(window=length, min_periods=length).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width})


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_length: int = 14,
    d_length: int = 3,
) -> pd.DataFrame:
    """Stochastic oscillator %%K and %%D."""
    lowest_low = low.rolling(window=k_length, min_periods=k_length).min()
    highest_high = high.rolling(window=k_length, min_periods=k_length).max()
    span = (highest_high - lowest_low).replace(0, np.nan)
    k = 100.0 * (close - lowest_low) / span
    d = k.rolling(window=d_length, min_periods=d_length).mean()
    return pd.DataFrame({"k": k, "d": d})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing via RMA)."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return _rma(tr, length)


def rolling_high(series: pd.Series, length: int, skip_last: int = 1) -> pd.Series:
    """Rolling max over the window *before* the current bar (Donchian upper)."""
    return series.shift(skip_last).rolling(window=length, min_periods=length).max()


def rolling_low(series: pd.Series, length: int, skip_last: int = 1) -> pd.Series:
    """Rolling min over the window *before* the current bar (Donchian lower)."""
    return series.shift(skip_last).rolling(window=length, min_periods=length).min()


def roc(series: pd.Series, length: int = 63) -> pd.Series:
    """Rate of change (percent) over `length` bars."""
    return (series / series.shift(length) - 1.0) * 100.0


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Williams %R — like an inverted stochastic, ranging -100 (oversold) to 0."""
    hh = high.rolling(window=length, min_periods=length).max()
    ll = low.rolling(window=length, min_periods=length).min()
    span = (hh - ll).replace(0, np.nan)
    return -100.0 * (hh - close) / span


def cci(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 20) -> pd.Series:
    """Commodity Channel Index using the standard 0.015 mean-deviation scaling."""
    tp = (high + low + close) / 3.0
    ma = tp.rolling(window=length, min_periods=length).mean()
    mean_dev = (tp - ma).abs().rolling(window=length, min_periods=length).mean()
    return (tp - ma) / (0.015 * mean_dev.replace(0, np.nan))


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_length: int = 20,
    atr_length: int = 20,
    mult: float = 2.0,
) -> pd.DataFrame:
    """Keltner Channels: EMA mid, ATR-scaled upper/lower envelopes."""
    mid = ema(close, ema_length)
    band = atr(high, low, close, atr_length)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * band, "lower": mid - mult * band})


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 10,
    mult: float = 3.0,
) -> pd.DataFrame:
    """
    Supertrend: ATR-band trend follower. Returns the trend line and an
    `uptrend` boolean (True while price rides above the line). Uses the
    canonical band-carry-forward recursion.
    """
    band = atr(high, low, close, length).to_numpy(dtype=float)
    hl2 = ((high + low) / 2.0).to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    n = len(c)

    upper = hl2 + mult * band
    lower = hl2 - mult * band
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend = np.full(n, np.nan)
    uptrend = np.zeros(n, dtype=bool)

    for i in range(n):
        if np.isnan(band[i]):
            continue
        if i == 0 or np.isnan(final_upper[i - 1]):
            final_upper[i], final_lower[i] = upper[i], lower[i]
            uptrend[i] = c[i] >= hl2[i]
        else:
            final_upper[i] = upper[i] if (upper[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
            final_lower[i] = lower[i] if (lower[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
            if c[i] > final_upper[i - 1]:
                uptrend[i] = True
            elif c[i] < final_lower[i - 1]:
                uptrend[i] = False
            else:
                uptrend[i] = uptrend[i - 1]
        trend[i] = final_lower[i] if uptrend[i] else final_upper[i]

    return pd.DataFrame(
        {"supertrend": trend, "uptrend": uptrend}, index=close.index
    )


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.DataFrame:
    """
    Average Directional Index with +DI/-DI (Wilder). Returns columns
    adx, plus_di, minus_di. RMA smoothing cancels between DM and TR, so
    +DI = 100·RMA(+DM)/ATR is the standard formulation.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    atr_line = atr(high, low, close, length).replace(0, np.nan)
    plus_di = 100.0 * _rma(plus_dm, length) / atr_line
    minus_di = 100.0 * _rma(minus_dm, length) / atr_line

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_line = _rma(dx, length)
    return pd.DataFrame({"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di})


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: running total of volume signed by daily close direction."""
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).fillna(0.0).cumsum()


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    length: int = 20,
) -> pd.Series:
    """Rolling volume-weighted average price over `length` bars.

    Crypto trades 24/7 with no session boundary, so a rolling VWAP (rather than a
    session-anchored one) is the sensible intraday reference.
    """
    typical = (high + low + close) / 3.0
    pv = (typical * volume).rolling(window=length, min_periods=length).sum()
    vol = volume.rolling(window=length, min_periods=length).sum().replace(0, np.nan)
    return pv / vol


def _money_flow_multiplier(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Where the close sits within the bar's range, mapped to [-1, +1].

    +1 = close on the high (all buying), -1 = close on the low (all selling).
    A doji/zero-range bar contributes nothing (NaN → 0 by the callers)."""
    rng = (high - low).replace(0, np.nan)
    return ((close - low) - (high - close)) / rng


def ad_line(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Accumulation/Distribution Line (Chaikin): running sum of money-flow volume.

    Volume signed by *where in the bar's range* price closed (not just up/down like
    OBV), so a heavy-volume close near the high reads as accumulation even on a red
    day. A rising A/D line is the classic footprint of steady institutional buying."""
    mfv = _money_flow_multiplier(high, low, close).fillna(0.0) * volume
    return mfv.fillna(0.0).cumsum()


def cmf(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, length: int = 21) -> pd.Series:
    """Chaikin Money Flow: money-flow volume summed over `length`, normalized by volume.

    Bounded roughly in [-1, +1]. Persistently > 0 means buyers have controlled the
    range for weeks — sustained net accumulation; < 0 is distribution."""
    mfv = _money_flow_multiplier(high, low, close).fillna(0.0) * volume
    vol_sum = volume.rolling(length, min_periods=length).sum().replace(0, np.nan)
    return mfv.rolling(length, min_periods=length).sum() / vol_sum


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, length: int = 14) -> pd.Series:
    """Money Flow Index: a volume-weighted RSI on typical price (0–100).

    Raw money flow (typical price × volume) is split into up/down days by the change
    in typical price; the index is the up-share of that flow. > 50 = money flowing in."""
    typical = (high + low + close) / 3.0
    raw_flow = typical * volume
    up = typical > typical.shift(1)
    down = typical < typical.shift(1)
    pos_flow = raw_flow.where(up, 0.0).rolling(length, min_periods=length).sum()
    neg_flow = raw_flow.where(down, 0.0).rolling(length, min_periods=length).sum()
    money_ratio = pos_flow / neg_flow.replace(0, np.nan)
    result = 100 - (100 / (1 + money_ratio))
    # All-up window → neg_flow 0 → ratio NaN → MFI saturates at 100.
    return result.where(neg_flow != 0, 100.0).where(pos_flow + neg_flow > 0, np.nan)
