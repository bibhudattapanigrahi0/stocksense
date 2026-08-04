# Strategy Catalog

Two separate things share the word "strategy" in StockSense:

- **Backtest strategies** (35) — time-series trading rules simulated over history. Defined
  in [`pipeline_core/backtest/strategies.py`](../pipeline_core/backtest/strategies.py). See
  [backtesting.md](backtesting.md).
- **Curated screening strategies** (10) — named filter bundles for point-in-time screening.
  Defined in [`pipeline_core/strategies.py`](../pipeline_core/strategies.py). See
  [screening-and-analysis.md](screening-and-analysis.md).

---

## Backtest strategies (35)

Long-only, computed purely from OHLCV. "Exit rules" shows the risk controls attached to
each `StrategyDef` (`Nd max` = time stop, `stop` = hard stop, `trail` = trailing stop,
`target` = take-profit, `min Nb` = warm-up bars). Entry/exit logic is summarized in the
description; the exact signal functions are in the source.

### Trend (8)

| ID | Name | Exit rules | Description |
|---|---|---|---|
| `golden-cross` | Golden Cross (50/200 DMA) | min 260b | Buy when the 50-DMA crosses above the 200-DMA; exit on the cross back down. |
| `ma-20-50-cross` | 20/50 DMA Cross | min 260b | Faster trend cross: 20-DMA above 50-DMA entry, cross below exit. |
| `dma200-regime` | 200-DMA Regime | min 260b | Buy when price crosses above a *rising* 200-DMA; exit on the cross below. |
| `trend-pullback-50dma` | Trend Pullback to 50-DMA | 30d max, min 260b | In an uptrend (above 200-DMA), buy the bounce back above the 50-DMA; exit if the 200-DMA breaks (or 30 days). |
| `ema-9-21-cross` | EMA 9/21 Crossover | min 60b | Fast exponential trend: buy when the 9-EMA crosses above the 21-EMA; exit on the cross back below. |
| `triple-ema-alignment` | Triple EMA Alignment (8/21/55) | min 120b | Buy when price reclaims the 8-EMA while 8>21>55 EMAs are stacked bullishly; exit when price loses the 21-EMA. |
| `supertrend-flip` | Supertrend Flip (10, 3) | min 60b | Buy when Supertrend(10, 3×ATR) flips to an uptrend; exit when it flips back down. |
| `adx-trend-rider` | ADX Trend Rider | 60d max, min 60b | Buy when +DI crosses above −DI with ADX > 25 (strong trend building); exit when −DI retakes +DI. |

### Momentum (7)

| ID | Name | Exit rules | Description |
|---|---|---|---|
| `rsi-momentum-thrust` | RSI Momentum Thrust | 20d max, min 260b | Buy when RSI crosses up through 60 (strength emerging); exit below 45 (or 20 days). |
| `macd-bull-cross-200dma` | MACD Bull Cross in Uptrend | min 260b | Buy MACD/signal bullish crosses only above the 200-DMA; exit on the bear cross. |
| `macd-histogram-reversal` | MACD Histogram Reversal | 25d max, min 260b | Buy when the MACD histogram turns positive after a negative streak; exit when it flips back. |
| `volume-surge-thrust` | Volume Surge Thrust | 10d max, 8% trail, min 260b | Buy +3% days on 2× average volume; exit with an 8% trailing stop or 10 days. |
| `gap-up-continuation` | Gap-Up Continuation | 5d max, 5% trail, min 260b | Buy >2% gap-ups with volume that hold above the open; 5% trailing stop, 5-day max hold. |
| `roc-momentum` | 63-Day ROC Momentum | 40d max, min 260b | Buy when quarterly rate-of-change crosses above +12% in an uptrend (above 200-DMA); exit when momentum turns negative (or 40 days). |
| `keltner-breakout` | Keltner Channel Breakout | 25d max, min 260b | Buy closes above the upper Keltner band (EMA20 + 2×ATR) in an uptrend; exit back at the mid EMA (or 25 days). |

### Mean Reversion (10)

| ID | Name | Exit rules | Description |
|---|---|---|---|
| `rsi-oversold-reversal` | RSI Oversold Reversal | 20d max, min 260b | Buy when RSI(14) drops below 30; exit when it recovers above 55 (or after 20 days). |
| `rsi-extreme-oversold` | RSI Extreme Oversold | 15d max, min 260b | Buy deep washouts (RSI < 25); exit at RSI 50 or 15 days. |
| `stochastic-oversold-cross` | Stochastic Oversold Cross | 15d max, min 260b | Buy when %K crosses above %D below 20; exit when %K reaches 80 (or 15 days). |
| `bollinger-lower-reversal` | Bollinger Lower Reversal | 15d max, min 260b | Buy when price closes back above the lower Bollinger Band; exit at the middle band (or 15 days). |
| `three-down-days` | Three Down Days Reversal | 12d max, +6% target, min 260b | Buy after 3 consecutive down closes with RSI < 40; exit at +6% target or 12 days. |
| `cci-reversal` | CCI Oversold Reversal | 15d max, min 60b | Buy when CCI(20) crosses back above −100 from oversold; exit when it reaches +100 (or 15 days). |
| `williams-r-oversold` | Williams %R Rebound | 12d max, min 60b | Buy when Williams %R(14) crosses up out of oversold (−80); exit when it recovers past −30 (or 12 days). |
| `bollinger-percent-b` | Bollinger %B Snapback | 12d max, min 60b | Buy washouts below the lower band (%B < 0.05) with RSI < 40; exit at the middle band (%B ≥ 0.5) or 12 days. |
| `connors-rsi2` | Connors RSI(2) | 10d max, min 260b | Larry Connors' short-term dip buy: RSI(2) < 10 while above the 200-DMA; exit when RSI(2) > 65 (or 10 days). |
| `gap-down-reversal` | Gap-Down Reversal | 10d max, +6% target, min 60b | Buy >3% gap-downs that close green (buyers stepped in) with RSI < 45; exit at +6% target or 10 days. |

### Breakout (8)

| ID | Name | Exit rules | Description |
|---|---|---|---|
| `fifty-two-week-high-breakout` | 52-Week High Breakout | 20d max, 10% trail, min 260b | Buy new 52-week highs on 1.5× volume; 10% trailing stop or 20 days. |
| `donchian-20-breakout` | Donchian 20 Breakout | min 60b | Buy closes above the prior 20-day high; exit on a close below the prior 10-day low. |
| `donchian-55-turtle` | Donchian 55 (Turtle) | min 80b | Classic turtle rule: buy 55-day highs, exit on 20-day lows. |
| `bollinger-upper-breakout` | Bollinger Upper Breakout | 20d max, min 260b | Buy closes above the upper band with volume confirmation; exit at the middle band. |
| `volatility-squeeze-breakout` | Volatility Squeeze Breakout | 20d max, min 160b | Buy when a 6-month band-width squeeze (lowest 10%) fires upward through the upper band; exit at the middle band. |
| `nr7-breakout` | NR7 Range Breakout | 10d max, 8% trail, min 40b | Buy the breakout above a narrowest-range-in-7-days bar (volatility coiling → expansion); 8% trailing stop or 10 days. |
| `inside-bar-breakout` | Inside Bar Breakout | 12d max, 8% trail, min 40b | Buy when price breaks above an inside-bar (range compression) high; 8% trailing stop or 12 days. |
| `volatility-contraction-breakout` | Volatility Contraction Breakout | 40d max, 12% trail, min 160b | Minervini-style VCP: buy a 50-day high breakout that fires while ATR% sits in its lowest quartile (tight base); 12% trailing stop or 40 days. |

### Volume (1)

| ID | Name | Exit rules | Description |
|---|---|---|---|
| `obv-breakout` | OBV 50-Day Breakout | min 90b | Accumulation signal: buy when On-Balance Volume makes a new 50-day high with price above the 50-DMA; exit below the 50-DMA. |

### Contrarian (1)

| ID | Name | Exit rules | Description |
|---|---|---|---|
| `fifty-two-week-low-reversal` | 52-Week Low Reversal | 25d max, min 260b | Buy within 5% of the 52-week low with RSI < 35; exit at RSI 50 or 25 days. |

> Which strategy tops the leaderboard depends on the `sort_by` metric and the run window —
> get live numbers from `GET /api/v1/backtest/leaderboard` or the `get_backtest_leaderboard`
> MCP tool. See [backtesting.md](backtesting.md).

---

## Curated screening strategies (10)

Named filter bundles for point-in-time screening (not backtested). Each maps to a
`ScreenFilters` set you can run via the Strategies tab, `GET /api/v1/strategies`, or the
`list_strategies` MCP tool.

| ID | Name | Thesis |
|---|---|---|
| `quality-compounders` | Quality Compounders | High-return, financially sound businesses with proven quality scores. |
| `value-hunters` | Value Hunters | Statistically cheap: low P/E, low P/B, with a dividend cushion. |
| `garp` | Growth at a Reasonable Price | Double-digit growers not priced for perfection (PEG ≤ 1.5). |
| `momentum-breakout` | Momentum Breakout | Uptrending stocks near 52-week highs with volume confirmation. |
| `oversold-quality` | Oversold Quality Reversal | Quality names in long-term uptrends that are short-term oversold. |
| `promoter-confidence` | Promoter Confidence | High promoter skin-in-the-game with negligible pledging. |
| `dividend-stalwarts` | Dividend Stalwarts | Reliable payers: 2%+ yield from large, financially steady companies. |
| `debt-free-cash-machines` | Debt-Free Cash Machines | Near-zero-debt companies generating serious free cash flow. |
| `large-cap-safety` | Large-Cap Safety | Blue-chip only: NIFTY 50 names with clean balance sheets. |
| `turnaround-watchlist` | Turnaround Watchlist | Beaten-down but returning to profit growth — higher risk/reward. |
