# Backtesting — the Strategy Lab

The Strategy Lab backtests a library of **35 long-only, price/volume-driven trading
strategies** over up to **10 years of Nifty 500** daily history, ranks them by risk/return
metrics, and reports which stocks are giving a **BUY / SELL / HOLDING** signal *right now*
under each strategy.

Everything is fully computable from OHLCV — the only data honestly backtestable a decade
back with our sources. (Fundamental-factor backtests are a documented future phase; see
`ENHANCEMENTS.md` §2.)

## Module layout (`pipeline_core/backtest/`)

| File | Role |
|---|---|
| [`strategies.py`](../pipeline_core/backtest/strategies.py) | The 35 `StrategyDef`s + their entry/exit signal functions. See [strategies.md](strategies.md). |
| [`engine.py`](../pipeline_core/backtest/engine.py) | Trade simulation, portfolio aggregation, and metrics. Pure functions, unit-tested. |
| [`data_loader.py`](../pipeline_core/backtest/data_loader.py) | Long-history OHLCV loader for the universe + benchmark, with a permanent-ish cache. |
| [`runner.py`](../pipeline_core/backtest/runner.py) | Orchestrates a full run, scans current signals, persists results, and exposes shared shaping helpers for the API/MCP/UI. |

## Execution model (no lookahead)

`engine.simulate()` runs one strategy on one stock:

- **Entry:** a signal on day *T* enters at day **T+1 open** (never the signal-day close).
- **Exit:** while in a position, conditions are checked on each close and executed at the
  **next open**. Same-day **priority** is:

  > `stop-loss` > `trailing-stop` > `target` > `time (max holding)` > `exit signal`

- **Costs:** a round-trip cost in basis points (default **25 bps** ≈ Indian delivery with
  STT + charges + slippage) is subtracted from each closed trade's return.
- **Open positions:** a position still open at the last bar is marked to the final close
  (half-cost applied) and reported separately as the current holding — never counted as a
  closed win/loss.
- **Warm-up:** each strategy declares `min_bars`; entry signals are ignored until that much
  history has accumulated, so indicators are trusted.

A `StrategyDef` declares the rules and risk controls:

| Field | Meaning |
|---|---|
| `entry(df)` / `exit(df)` | Boolean signal series (True on the signal day). |
| `stop_loss_pct` | Hard stop from entry. |
| `trailing_stop_pct` | Trailing stop from the peak close. |
| `target_pct` | Take-profit. |
| `max_holding_days` | Time stop. |
| `min_bars` | Warm-up bars before signals count (default 260). |

## Portfolio aggregation

The equity curve is a **documented approximation of signal quality**, not a
capital-constrained portfolio:

> daily portfolio return = the **equal-weight mean** of the daily returns of all positions
> active that day (flat when nothing is active; unlimited fragmentation).

This isolates "how good are the signals" from position-sizing and capital-allocation
choices. It is close-to-close marked (entries/exits at open are approximated to close).

## Metrics

`engine.compute_metrics()` produces, per strategy:

| Metric | Meaning |
|---|---|
| `trades` | Number of closed trades. |
| `win_rate` | % of closed trades with a positive net return. |
| `avg_win_pct` / `avg_loss_pct` | Mean return of winners / losers. |
| `profit_factor` | Gross wins ÷ gross losses (`"inf"` if no losses). |
| `expectancy_pct` | Mean return per trade. |
| `avg_holding_days` | Mean holding period. |
| `total_return_pct` | Cumulative return of the equity curve. |
| `cagr_pct` | Compound annual growth rate of the equity curve. |
| `max_drawdown_pct` | Worst peak-to-trough decline (negative). |
| `sharpe` | Annualized mean/σ of daily returns (rf = 0). |
| `sortino` | Like Sharpe but downside deviation only. |
| `calmar` | CAGR ÷ \|max drawdown\|. |
| `ann_volatility_pct` | Annualized volatility. |
| `benchmark_cagr_pct` | NIFTY (`^NSEI`) CAGR over the same window. |
| `alpha_vs_benchmark_pct` | Strategy CAGR − benchmark CAGR. |

## Current signals (BUY / SELL / HOLDING)

`runner.scan_current_signals()` re-simulates each strategy and classifies each stock as of
the latest bar, using a short `lookback_days` window (default 3):

- **BUY** — a fresh entry edge fired within the lookback window and there's no open trade.
  Payload: `ticker`, `signal_date`, `price`.
- **SELL** — a position closed (by any exit) within the lookback window. Payload: `ticker`,
  `exit_date`, `exit_reason`, `ret_pct`, `days_held`.
- **HOLDING** — an open position right now. Payload: `ticker`, `entry_date`, `entry_price`,
  `unrealized_pct`, `days_held`.

This is how you answer *"which stocks is strategy X buying today?"* — via
`get_backtest_strategy` (one strategy) or `get_backtest_signals` (all strategies).

## Trade ideas (`plan`)

`strategy_detail(id)` and `strategy_library()` also return a **`plan`** object — the
strategy's own risk controls: `stop_loss_pct`, `trailing_stop_pct`, `target_pct`,
`max_holding_days`, `min_bars`. The Strategy Lab UI combines each fresh **BUY** signal
with this `plan` to render an actionable **Trade Idea**: entry (fills at the next
session's open, referenced off the latest close), a stop level (`price × (1 − stop_loss_pct)`),
a target level (`price × (1 + target_pct)`) or trailing-stop note, the time stop, and the
reward:risk ratio when both a hard stop and target exist. Strategies that exit purely on a
signal (no hard levels) show *"exit on strategy signal"*. Levels are indicative
decision-support, not order instructions.

## Trade ideas by horizon (short / medium / long term)

`runner.horizon_ideas(max_per_bucket=20)` rolls the current BUY signals up across all
strategies into three time-horizon buckets. Each strategy is classified by
`strategy_horizon()` from its holding period:

| Horizon | Rule | Character |
|---|---|---|
| **short** | time stop ≤ 15 days | mean-reversion snapbacks, quick momentum/breakouts |
| **medium** | time stop 16–40 days | multi-week momentum / breakout / volume setups |
| **long** | time stop > 40 days, or **no** time stop (exits on a trend/structure signal) | trend-following systems |

Within each bucket the buys are **aggregated by stock**: a name flagged by more strategies
in that horizon scores higher (conviction). Buckets are ranked by conviction, then average
strategy win rate, then average Sharpe, and capped at `max_per_bucket` (default 20, max 50).
Each candidate carries its `price`, `conviction`, `avg_win_rate`, `latest_signal_date`, and
the list of `strategies` that flagged it. Exposed as `GET /api/v1/backtest/ideas` and the
`get_trade_ideas` MCP tool, and rendered in the dashboard's **Trade Ideas** tab.

**Every idea has a stop-loss.** `stop_price`/`stop_pct` come from the *tightest* stop the
flagging strategies actually define (a hard `stop_loss_pct` or `trailing_stop_pct`); if none
define one, a horizon default is used (`HORIZON_DEFAULT_STOP` — short 6%, medium 9%, long 13%),
flagged via `stop_source`.

### Trade ledger — open positions, past trades, realized track record

Ideas are recorded so their outcome can be followed as a **lifecycle**, not just a live mark.
`snapshot_current_ideas()` runs automatically at the end of every backtest (and on demand via
`POST /api/v1/backtest/ideas/track`), appending each idea to `data/backtests/tracked_ideas.json`
— idempotent per `(ticker, horizon, market)`. Each idea carries entry, `stop_price`,
`target_price`, and `horizon`.

`tracked_ideas_status(market)` then walks each **open** idea's daily OHLC forward from its signal
day (`market_data.get_price_history`) and **closes** it the first time it:

- hits its **stop** (`Low ≤ stop_price`) → recorded loss,
- hits its **target** (`High ≥ target_price`) → recorded win (stop takes precedence in a bar that
  spans both), or
- **expires** past its horizon time-stop (`HORIZON_MAX_BARS` = short 15 / medium 40 / long 120
  trading days) → closed at that day's price.

A closed trade is written back once (idempotent) with `exit_reason`, `exit_price`, `exit_date`,
and `realized_return_pct` — it moves to **past trades** and never flips back on a later price
move. Still-open ideas are live-marked (winning / losing / flat). The response splits `open` vs
`closed`, with a **realized scoreboard** (win rate = `wins ÷ (wins + losses)` on *closed* trades,
plus targets / stops / expiries and total realized P&L) and an `equity_curve` (cumulative realized
P&L over exit dates — an equal-weight "took every idea" book). Exposed as
`GET /api/v1/backtest/ideas/tracked` and the `get_tracked_ideas` MCP tool; `DELETE …/ideas/tracked`
resets it. Prices use magnitude-aware rounding (`utils.round_price`) so sub-dollar crypto keeps
meaningful stops/targets.

## Data & caching

`data_loader.py` loads up to 10 y of daily OHLCV per symbol via yfinance:

- Historical bars are effectively immutable, so frames are cached for **20 hours**
  (`BT_CACHE_TTL`) in `data/cache/backtest` — a refresh only picks up the latest completed
  session. This is what makes re-runs fast.
- Symbols with fewer than **`MIN_BARS = 300`** bars (recent listings) are **skipped** and
  counted, never fatal. A failed symbol degrades to "failed" in the stats.
- Loading is bounded by `ingestion_concurrency`. Benchmark is the selected market's
  (`^NSEI`/NIFTY 50 for IN, `^GSPC`/S&P 500 for US, gold for metals, Bitcoin for crypto).

A typical NIFTY 500 run loads ~475/500 symbols (25 skipped for short history).

> **Multi-market:** the backtest is market-aware — pass `market` (`IN`/`US`/`METALS`/
> `CRYPTO`) to run against that universe + benchmark. Results are stored **per market**
> in `data/backtests/latest_<MARKET>.json`, and the leaderboard / signals / ideas / status
> reads take the same `market` argument. See [markets.md](markets.md).

## Running a backtest

The run loads history (cached), backtests every strategy, scans current signals, and
persists to `data/backtests/latest_<MARKET>.json` (e.g. `latest_IN.json`).

- **Dashboard:** Strategy Lab tab → **⟳ Run Backtests** (runs for the header-selected
  market; progress bar polls `/api/v1/backtest/status?market=…`).
- **REST:** `POST /api/v1/backtest/run` with `{market, index, years, cost_roundtrip_bps,
  strategy_ids?}` → then `GET /api/v1/backtest/leaderboard?sort_by=…&market=…`.
- **MCP:** `run_backtest(market=…)` → poll `get_backtest_status(market=…)` →
  `get_backtest_leaderboard` / `get_backtest_signals` / `get_backtest_strategy` (all take `market`).
- **Script:**

  ```bash
  env/bin/python -c "import asyncio; from pipeline_core.backtest.runner import run_full_backtest; \
  r=asyncio.run(run_full_backtest('NIFTY 500', 10)); print(len(r['strategies']),'strategies', r['elapsed_seconds'],'s')"
  ```

**Performance:** first run downloads ~500 stocks' history (minutes, network-bound);
subsequent runs are cache-served and complete in **~75–140 s** for all 38 strategies.
Only one run may be in flight at a time (`job_state["running"]` guard).

## Output structure (`latest.json`)

```jsonc
{
  "run_at": "…", "index": "NIFTY 500", "years": 10,
  "cost_roundtrip_bps": 25.0, "elapsed_seconds": 136.9,
  "data_stats": { "total": 500, "loaded": 475, "skipped_insufficient": 25, "failed": 0 },
  "benchmark_equity": [[ts_ms, value], …],          // downsampled ~400 pts
  "strategies": [
    {
      "id": "…", "name": "…", "category": "…", "description": "…",
      "metrics": { … },                              // the metrics table above
      "stocks_traded": 470, "open_positions": 120,
      "equity": [[ts_ms, value], …],                 // downsampled equity curve
      "signals": { "buy": [...], "sell": [...], "holding": [...] },
      "sample_trades": [ …best 5… ], "worst_trades": [ …worst 5… ]
    }
  ],
  "disclaimer": "Backtest caveats: …"
}
```

The API/MCP read this file through shared helpers — `leaderboard(sort_by)`,
`strategy_detail(id)`, `signals_summary(signal_type?)`, `strategy_library()` — so the REST
app, the MCP server, and the UI all present identical data.

## Biases & caveats (declared on every result)

The `BIAS_DISCLAIMER` rides on every payload:

- **Survivorship bias** — tested on **current** Nifty 500 constituents; stocks that crashed
  out of the index are absent.
- **Long-only**, next-open execution, 25 bps round-trip costs.
- The portfolio curve is an equal-weight **signal-quality** measure, not a capital-constrained
  portfolio.
- No delisting data; annual-statement granularity is why fundamentals are excluded.
- **Past performance ≠ future results. Decision-support research, not investment advice.**

## Adding a strategy

1. **Signals** — add entry/exit as named functions in `strategies.py` (reuse
   `pipeline_core/agents/indicators.py`; if you need a new indicator, add it there with a
   unit test). Use the helpers `_crosses_above/_below`, `_fill`, `_never`.
2. **Definition** — append a `StrategyDef` to the `STRATEGIES` list with a unique `id`,
   `name`, `category`, `description`, the entry/exit functions, and any risk controls
   (`stop_loss_pct`, `trailing_stop_pct`, `target_pct`, `max_holding_days`, `min_bars`).
3. **Tests** — `test_strategies.py` auto-parametrizes over `STRATEGIES`, so a new strategy
   is validated (unique id, boolean/aligned signals, coherent trades) with no extra test.
   Add a dedicated indicator test if you introduced one.
4. **Re-run** the backtest so `latest.json` (and the UI/API/MCP) include it. The UI reads
   the strategy count dynamically — no frontend change needed.

Example skeleton:

```python
def _my_entry(df):
    c = df["Close"]
    return _fill(_crosses_above(ind.ema(c, 10), ind.ema(c, 30)))

def _my_exit(df):
    c = df["Close"]
    return _fill(_crosses_below(ind.ema(c, 10), ind.ema(c, 30)))

StrategyDef(
    id="ema-10-30-cross", name="EMA 10/30 Crossover", category="Trend",
    description="Buy 10-EMA over 30-EMA; exit on the cross back down.",
    entry=_my_entry, exit=_my_exit, min_bars=60,
)
```
