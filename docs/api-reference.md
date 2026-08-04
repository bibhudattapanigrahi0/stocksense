# REST API Reference

Served by [`app.py`](../app.py) (FastAPI). Base URL when run locally:
`http://127.0.0.1:8000`. Interactive docs at **`/docs`** (Swagger) and **`/redoc`**.
CORS is open (`*`). All endpoints are under `/api/v1`. Responses are JSON unless noted.

Run it: `env/bin/uvicorn app:app --reload --port 8000`.

---

## Pipeline

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/pipeline/run` | Start a full run in the background. Body: `PipelineRequest`. → `{run_id, status, ws}`. |
| `POST` | `/api/v1/pipeline/run/sync` | Run to completion and return the full `PipelineRun` (long request). |
| `GET` | `/api/v1/pipeline/runs?limit=20` | Recent run history (from SQLite). |
| `GET` | `/api/v1/pipeline/{run_id}` | Status + results for a run (active or persisted). |
| `WS` | `/api/v1/pipeline/{run_id}/ws` | Live progress events; send text as keepalive. |

**`PipelineRequest`**: `{ query?, filters?: ScreenFilters, analysis_depth?: "quick"|"standard"|"deep",
max_candidates?: 1–50, include_report?: bool }`. See
[screening-and-analysis.md](screening-and-analysis.md) for filter fields.

Progress event shape: `{ pipeline_id, stage, progress_pct, candidates_found, status? }`.

---

## Screening

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/screen` | Screen-only (no deep analysis). Body: `PipelineRequest`. → `{filters_applied, count, candidates[], llm_mode}`. |

---

## Stocks

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/stocks/search?q=` | Ticker/name search (direct match + NIFTY 50). → `[{symbol, name, sector}]`. |
| `GET` | `/api/v1/stocks/suggest?q=&limit=10` | Fast ticker **autocomplete** over the cached NIFTY 500 universe (prefix-first, no per-ticker network). → `[{symbol}]`. Powers the Analyze tab typeahead. |
| `GET` | `/api/v1/stocks/{ticker}` | Detailed NSE-enriched profile (`StockDetail`). |
| `GET` | `/api/v1/stocks/{ticker}/analyze?depth=standard` | Four-agent deep dive → composite + sub-scores + evidence. |
| `POST` | `/api/v1/stocks/{ticker}/report` | Run analysis fresh and render a markdown report. → `{ticker, filename, markdown}`. |

---

## Reports

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/reports?limit=50` | List generated report files, newest first. |
| `GET` | `/api/v1/reports/{filename}` | Raw markdown text (path-traversal-safe; `.md` only). |

---

## Saved screens

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/screens` | List saved filter presets. |
| `POST` | `/api/v1/screens` | Save a preset. Body: `SavedScreen`. (201) |
| `DELETE` | `/api/v1/screens/{name}` | Delete a preset. |

---

## Curated strategies

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/strategies` | Curated **screening** strategies (filter bundles). See [strategies.md](strategies.md#curated-screening-strategies). |

---

## Backtesting

All read from the last persisted run (`data/backtests/latest.json`) except `run`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/backtest/run` | Start a background backtest. Body below. → `{status, strategies}`. `409` if one is already running. |
| `GET` | `/api/v1/backtest/status` | Job state (`running`, `stage`, `progress_pct`, `error`) + last-run summary. |
| `GET` | `/api/v1/backtest/leaderboard?sort_by=sharpe` | All strategies + metrics + signal counts, sorted. |
| `GET` | `/api/v1/backtest/strategy/{strategy_id}` | Full detail: metrics, equity curve, signals, best/worst trades, and `plan` (the strategy's stop/target/trail/time-stop rules, used to build trade ideas). `404` if no run / unknown id. |
| `GET` | `/api/v1/backtest/signals` | Current BUY/SELL/HOLDING signals for every strategy. |
| `GET` | `/api/v1/backtest/strategies` | Static strategy catalog (available before any run). |
| `GET` | `/api/v1/backtest/ideas?max_per_bucket=20` | Current buys grouped into **short / medium / long term** horizons, aggregated by stock and ranked by conviction (how many strategies agree). Each idea carries a **stop-loss** (`stop_price`, `stop_pct`, `stop_source`). Each bucket capped at `max_per_bucket` (default 20, max 50). |
| `GET` | `/api/v1/backtest/ideas/tracked` | Tracked ideas priced live → per-idea return % + status (`winning`/`losing`/`stopped_out`/`flat`) and a scoreboard (winning vs losing, win rate, avg P&L). |
| `POST` | `/api/v1/backtest/ideas/track?max_per_bucket=20` | Snapshot the current ideas into the tracker (idempotent per ticker+horizon). Also runs automatically after every backtest. |
| `DELETE` | `/api/v1/backtest/ideas/tracked` | Reset the tracker (forget all tracked ideas). |

**Run body** (`BacktestRunRequest`):

```jsonc
{
  "index": "NIFTY 500",        // default
  "years": 10,                 // 2–15
  "cost_roundtrip_bps": 25.0,  // 0–200
  "strategy_ids": null         // null = all 38, or a subset of ids
}
```

**`sort_by`** ∈ `sharpe` (default), `win_rate`, `cagr`, `max_drawdown`, `profit_factor`,
`expectancy`, `calmar`, `alpha`, `sortino`, `total_return`. (`max_drawdown` sorts
less-negative-first, i.e. best first.) See [backtesting.md](backtesting.md) for metric
definitions and the response shape.

Example:

```bash
curl -X POST localhost:8000/api/v1/backtest/run \
  -H 'content-type: application/json' \
  -d '{"index":"NIFTY 500","years":10}'

# poll, then:
curl 'localhost:8000/api/v1/backtest/leaderboard?sort_by=win_rate'
curl  localhost:8000/api/v1/backtest/strategy/connors-rsi2
```

---

## Portfolios

Construct and manage **model portfolios** from the strategies. See [portfolios.md](portfolios.md).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/portfolios/config` | Risk presets, weighting schemes, basis options. |
| `GET` | `/api/v1/portfolios/themes` | Curated theme catalog. |
| `POST` | `/api/v1/portfolios/construct` | Construct from a `PortfolioSpec` → portfolio (preview, not saved). `400` on bad spec / no backtest for systematic. |
| `POST` | `/api/v1/portfolios` | Construct **and save** → `201`. |
| `GET` | `/api/v1/portfolios` | List saved portfolios (newest first). |
| `GET` | `/api/v1/portfolios/{id}` | One saved portfolio by id or name. `404` if missing. |
| `POST` | `/api/v1/portfolios/{id}/rebalance` | Buy/trim/exit actions to reach the fresh target. |
| `DELETE` | `/api/v1/portfolios/{id}` | Delete a saved portfolio. |

**Spec body** (`PortfolioSpec`):

```jsonc
{
  "name": "Green Quality",
  "basis": "blend",                // fundamental | systematic | blend
  "strategy_ids": ["quality-compounders", "golden-cross"],
  "theme": "green-energy",         // slug from /themes, or null
  "risk": "balanced",              // conservative | balanced | aggressive
  "risk_overrides": null,          // optional partial RiskProfile overrides
  "weighting": "score",            // equal | score | conviction
  "max_positions": null            // optional override of the risk preset
}
```

Example:

```bash
curl -X POST localhost:8000/api/v1/portfolios/construct \
  -H 'content-type: application/json' \
  -d '{"name":"Green Quality","basis":"fundamental","strategy_ids":["quality-compounders"],"theme":"green-energy","risk":"balanced"}'
```

`systematic` / `blend` bases need a completed backtest first (`POST /api/v1/backtest/run`).

---

## Health & dashboard

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/health` | `{app, version, llm_mode, default_index}`. |
| `GET` | `/` | Dashboard SPA (`static/index.html`). |
| — | `/static/*` | Static assets. |
