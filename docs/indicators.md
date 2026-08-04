# Technical Indicators

All indicators are pure `pandas`/`numpy` implementations in
[`pipeline_core/agents/indicators.py`](../pipeline_core/agents/indicators.py) — owned
in-repo (no `pandas-ta` dependency) so they're small, deterministic, and unit-tested
(`tests/test_indicators.py`). They power both the screening technicals and the backtest
strategies.

**Conventions**
- Inputs are `pd.Series` (or OHLC series); outputs preserve the input index.
- Warm-up periods return `NaN` (via `min_periods=length`) rather than partial values.
- **Wilder's smoothing (RMA)** is used where charting platforms use it (RSI, ATR, ADX), so
  values match TradingView / StockCharts. The internal `_rma()` helper uses an SMA seed.

## Moving averages

| Function | Signature | Notes |
|---|---|---|
| `sma` | `sma(series, length)` | Simple moving average. |
| `ema` | `ema(series, length)` | Exponential MA, `adjust=False` (first value = first observation). |

## Momentum & oscillators

| Function | Signature | Range / output | Notes |
|---|---|---|---|
| `rsi` | `rsi(close, length=14)` | 0–100 | Wilder RSI. All-gains ⇒ 100, all-losses ⇒ ~0. Used at `length=2` for Connors RSI(2). |
| `macd` | `macd(close, fast=12, slow=26, signal=9)` | DataFrame `macd, histogram, signal` | Histogram = macd − signal. |
| `stochastic` | `stochastic(high, low, close, k_length=14, d_length=3)` | DataFrame `k, d` | %K / %D oscillator. |
| `williams_r` | `williams_r(high, low, close, length=14)` | −100…0 | Inverted stochastic; near 0 = overbought, near −100 = oversold. |
| `cci` | `cci(high, low, close, length=20)` | unbounded | Commodity Channel Index, standard 0.015 mean-deviation scaling. |
| `roc` | `roc(series, length=63)` | percent | Rate of change over `length` bars. |

## Volatility & channels

| Function | Signature | Output | Notes |
|---|---|---|---|
| `bollinger_bands` | `bollinger_bands(close, length=20, num_std=2.0)` | `mid, upper, lower, width` | `width` is relative `(upper−lower)/mid`; `%B = (close−lower)/(upper−lower)`. |
| `atr` | `atr(high, low, close, length=14)` | Series | Average True Range (Wilder/RMA). |
| `keltner_channels` | `keltner_channels(high, low, close, ema_length=20, atr_length=20, mult=2.0)` | `mid, upper, lower` | EMA mid ± `mult`·ATR. |
| `annualized_volatility_pct` | `annualized_volatility_pct(close, trading_days=252)` | float % | Annualized σ of daily returns. |

## Trend systems

| Function | Signature | Output | Notes |
|---|---|---|---|
| `supertrend` | `supertrend(high, low, close, length=10, mult=3.0)` | `supertrend, uptrend` | ATR-band trend follower; `uptrend` is a boolean the strategies watch for flips. Canonical band-carry-forward recursion. |
| `adx` | `adx(high, low, close, length=14)` | `adx, plus_di, minus_di` | Wilder ADX/DMI. `+DI = 100·RMA(+DM)/ATR` (RMA cancels between DM and TR). |

## Volume

| Function | Signature | Output | Notes |
|---|---|---|---|
| `obv` | `obv(close, volume)` | Series | On-Balance Volume: running total of volume signed by daily close direction. |

## Channels (Donchian)

| Function | Signature | Notes |
|---|---|---|
| `rolling_high` | `rolling_high(series, length, skip_last=1)` | Rolling max over the window *before* the current bar (Donchian upper). `skip_last=0` includes the current bar. |
| `rolling_low` | `rolling_low(series, length, skip_last=1)` | Rolling min over the prior window (Donchian lower). |

## Risk / relative

| Function | Signature | Notes |
|---|---|---|
| `beta_vs_benchmark` | `beta_vs_benchmark(close, benchmark_close, trading_days=252)` | Beta from aligned daily returns; `None` when < 30 overlapping points. |

## Which strategies use what

| Indicator | Backtest strategies |
|---|---|
| SMA / EMA | Golden Cross, 20/50 & 200-DMA, EMA 9/21, Triple-EMA, pullbacks |
| RSI | RSI reversals & thrust, Connors RSI(2), gap reversals |
| MACD | MACD cross / histogram |
| Bollinger | Lower/upper/%B, squeeze |
| Stochastic | Stochastic oversold cross |
| ATR / Keltner | Keltner breakout, Supertrend, volatility-contraction |
| Supertrend | Supertrend Flip |
| ADX/DMI | ADX Trend Rider |
| CCI / Williams %R | CCI reversal, Williams %R rebound |
| ROC | 63-day ROC momentum |
| OBV | OBV breakout |
| Donchian (`rolling_high/low`) | Donchian 20 / 55, 52-week high/low, NR7 |

See [backtesting.md → Adding a strategy](backtesting.md#adding-a-strategy) for how to add an
indicator and wire it into a new strategy.
