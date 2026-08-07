# StockSense — Agentic Stock Filter & Analysis Pipeline

Agentic screening and multi-agent analysis across **four markets — India (NSE/BSE),
US (NYSE/NASDAQ), metals, and crypto** — exposed two ways:

- **MCP server** (`server.py`) — tools for Claude Desktop / any MCP client
- **FastAPI app** (`app.py`) — REST + WebSocket for dashboards

Pick the market with a header switcher (dashboard) or a `market` parameter (REST/MCP);
equity markets (IN/US) run the full fundamentals pipeline, while price-only markets
(metals/crypto) use a technical/momentum composite. See [`docs/markets.md`](docs/markets.md).

Both sit on the shared **`pipeline_core`** engine: 8 cooperating agents, staged
deterministic screening, parallel deep-dive analysis, configurable score fusion,
SQLite run persistence, and markdown equity-research reports.

> Outputs are decision-support research, **not investment advice**.

**📚 Full documentation lives in [`docs/`](docs/README.md)** — [architecture](docs/architecture.md),
[screening & analysis](docs/screening-and-analysis.md), the [Strategy Lab backtester](docs/backtesting.md),
the [strategy catalog](docs/strategies.md), [portfolio management](docs/portfolios.md),
[indicators](docs/indicators.md), and the [REST](docs/api-reference.md) + [MCP](docs/mcp-tools.md) references.

---

## Architecture

```
┌─────────────────────┐   ┌──────────────────────┐
│  MCP server         │   │  FastAPI app         │
│  server.py (FastMCP)│   │  app.py (REST + WS)  │
└─────────┬───────────┘   └──────────┬───────────┘
          └────────────┬─────────────┘
                       ▼
            pipeline_core (shared engine)
 ┌─────────────────────────────────────────────────┐
 │ OrchestratorAgent                               │
 │  intent → universe → ingest → screen → fan-out  │
 │  analyze → fuse & rank → report → persist       │
 └───┬──────┬──────┬──────┬──────┬──────┬──────────┘
     ▼      ▼      ▼      ▼      ▼      ▼
  Ingest  Screen  Fund.  Tech.  Sent.  Risk   + ReportGenerator
     │      │      │      │      │      │
     ▼      ▼      ▼      ▼      ▼      ▼
  services: market_data (yfinance) · nse_data · llm_service · run_store
  storage:  diskcache (TTL) · SQLite (runs, saved screens, portfolios)
```

### Pipeline stages

1. **Intent** — natural-language query → `ScreenFilters` via schema-validated LLM
   structured output (one retry); safe no-op when no LLM is configured.
2. **Universe** — live index constituents from Nifty Indices CSV (NIFTY 50/100/200/500,
   NEXT 50, BANK, IT, MIDCAP 150, SMALLCAP 250), hardcoded NIFTY 50 fallback.
3. **Ingest** — batched profile fetch, semaphore-bounded (`ingestion_concurrency`, default 8).
4. **Screen** — staged filter registry: cheap `info` filters across the universe, then
   `nse` (promoter/pledge/delivery), `technical` (RSI/DMA/MACD/52w/volume), and `quality`
   (ROCE/Piotroski/coverage/3y CAGR) filters on survivors only (≤ 3× max_candidates).
   Missing data ⇒ filter is *indeterminate* — excluded from scoring, never counted against.
5. **Analyze** — per candidate, one shared `StockContext` (profile + prices + statements +
   benchmark + news) feeds four agents concurrently: fundamental, technical, sentiment, risk.
6. **Fuse** — composite = 0.40·fundamental + 0.30·technical + 0.15·sentiment + 0.15·(100−risk);
   weights configurable in settings.
7. **Report** — markdown research note for top-N picks. Template-first: always renders,
   LLM only writes the executive summary when configured.
8. **Persist** — every run saved to SQLite (`data/stocksense.db`); history survives restarts.

### LLM policy

Deterministic-first: filters, indicators, scores are pure Python. LLM is used only for
NL intent parsing, moat/sentiment narrative, and the report's executive summary.
With `llm_provider=none` (default) everything runs **rules-only** — a lexicon sentiment
scorer and template summaries. Supported providers: `openai`, `anthropic`, `ollama`.

---

## Setup

```bash
# Python 3.13 venv already at ./env (or create your own)
env/bin/pip install -r requirements.txt

# optional: enable LLM features
cp .env.example .env   # set llm_provider=openai + openai_api_key=...
```

## Run the MCP server

```bash
env/bin/python server.py                 # stdio (Claude Desktop)
env/bin/python server.py --http --port 8001   # streamable HTTP
```

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "stocksense": {
      "command": "/Users/bdpanigrahi/projects/mcp_projects/stock-data/env/bin/python",
      "args": ["/Users/bdpanigrahi/projects/mcp_projects/stock-data/server.py"]
    }
  }
}
```

### MCP tools

All stock/analysis/backtest tools take a `market` argument (`IN` | `US` | `METALS` | `CRYPTO`, default `IN`).

| Tool | What it does |
|---|---|
| `list_markets` | Supported markets + their universes, benchmark, units, and whether fundamentals apply |
| `screen_stocks` | Filters and/or NL query → ranked candidates with per-filter explanations (market-scoped) |
| `analyze_stock` | Deep dive on one ticker → composite + sub-scores + evidence (four-agent for equities; technical/momentum for metals/crypto) |
| `get_stock_overview` | Fast profile + technicals (NSE data for IN; no LLM) |
| `run_pipeline` | Full pipeline in background → `run_id` (universe-scale jobs) |
| `get_pipeline_status` | Poll a run's progress/results |
| `list_pipeline_runs` | Recent run history |
| `generate_report` | Markdown research note for one ticker |
| `save_screen` / `list_saved_screens` / `get_saved_screen` / `delete_saved_screen` | Filter presets |
| `get_index_constituents_list` | Current constituents of an index |
| `list_strategies` | Curated screening strategies (filter bundles) |
| `list_backtest_strategies` · `run_backtest` · `get_backtest_status` | The 38-strategy Strategy Lab: catalog, start a run, poll progress |
| `get_backtest_leaderboard` · `get_backtest_strategy` · `get_backtest_signals` | Ranked metrics; one strategy's detail + BUY/SELL/HOLDING stocks; all current signals |
| `get_trade_ideas` | Highest-conviction buys grouped into short / medium / long term horizons (≤ 20 each), each with a stop-loss |
| `get_trade_basket` · `track_trade_basket` | **Today's Basket**: *"just tell me what to buy right now"* — the whole board distilled into a deployable best-of of 3–5 trades, sized as one ¼-Kelly book (weights ≤100%), diversified, regime-scaled, sticky day-to-day; `track_trade_basket` commits + tracks the picks |
| `deploy_trade_basket` · `run_basket_backtest` · `get_basket_backtest` | Deploy the basket as a **NAV-tracked portfolio** (idempotent, cost-basis-preserving); and **backtest the construction method** — held-basket, monthly-rebalanced, point-in-time → CAGR/Sharpe/maxDD/alpha vs benchmark |
| `get_hold_basket` · `commit_hold_basket` · `apply_hold_basket` | **My Hold Basket**: a committed, conviction-held, low-churn book — commit once, then it only changes on a **material** event (stop/target hit, thesis broke, or a dramatically-better idea), shown as a reasoned diff you apply |
| `get_tracked_ideas` | Trade ledger: open positions + past trades (closed on stop/target/expiry) with realized win rate, per-idea P&L, and an idea-book equity curve |
| `construct_portfolio` · `list_portfolios` · `get_portfolio` · `snapshot_portfolio` · `rebalance_portfolio` · `delete_portfolio` | **Portfolio management**: build a risk-shaped, theme-tilted basket from one or more strategies; save, list, mark-to-market (NAV over time), rebalance |
| `list_risk_profiles` · `list_portfolio_themes` | Risk presets (conservative/balanced/aggressive) and curated themes (AI, green energy, EV, defense, …) |
| `list_bots` · `crypto_intraday_ideas` · `scan_crypto_bot` | **Intraday crypto bots (long + short)**: `crypto_intraday_ideas` aggregates the whole bidirectional signal-bot family by conviction on a 5m–4h timeframe → ranked **long and short** Trade Ideas; `scan_crypto_bot` runs one bot (breakout / mean-reversion / ema-trend / vwap-reclaim / mtf-confluence) → coins signaling long/short now with side + entry/stop/target/R |
| `run_intraday_backtest` · `get_intraday_bot_edge` | **Bot edge**: triple-barrier walk-forward that measures each intraday bot's win-rate / expectancy / profit-factor per timeframe × regime (net of costs); the edge then weights Trade-Ideas conviction |
| `run_crypto_momentum_bot` | **Crypto Momentum Bot**: BTC-regime-gated, inverse-vol-weighted top-N momentum basket (all-cash when BTC is below trend); `ai=true` adds an LLM risk review (briefing/conviction/flags, rules-only fallback); `deploy=true` saves it as a NAV-tracked portfolio |
| `health` | Config/health info |

Resources: `report://{filename}`, `screen://{name}`.
Full tool reference: [docs/mcp-tools.md](docs/mcp-tools.md).

## Run the REST API + Dashboard

```bash
env/bin/uvicorn app:app --reload --port 8000
# dashboard at http://127.0.0.1:8000/  ·  API docs at /docs
```

The dashboard (vanilla JS SPA in `static/`) sits behind a **login** — each account's
data (portfolios, watchlist, paper, saved screens, alerts) is private; the first sign-up
claims the **owner** account (which the MCP server and background loop also act as, and
which inherits any pre-accounts data). Auth is stdlib-only (`hashlib.scrypt` + server-side
session cookie — no extra deps); see the `auth_*` / `owner_*` settings in `.env.example`.

It opens on a personalized **Home** — a time-of-day greeting, quick actions, and
at-a-glance widgets (top trade ideas, watchlist, paper account, portfolios) — and ships a
polished **light + dark theme** with a top-bar toggle plus a **Settings** panel (theme,
accent, density, default market, landing page, profile) whose preferences follow the
account across devices. Then the workflow tabs:

- **Screener** — NL query + grouped filter form (universe, valuation, profitability,
  India-specific, technicals) → candidate cards with match %, key metrics, and
  expandable per-filter pass/fail evidence chips. Save/load filter presets.
- **Strategies** — one-click curated screening strategies (Quality Compounders, Value
  Hunters, Momentum Breakout, …).
- **Strategy Lab** — 10-year backtest leaderboard for all 38 trading strategies; click a
  row for metrics, an equity-vs-NIFTY chart, per-strategy **Trade Ideas** (entry/stop/target
  from the strategy's own rules), and BUY/SELL/HOLDING stock lists. See
  [docs/backtesting.md](docs/backtesting.md).
- **Trade Ideas** — the backtest's current buy signals rolled up into **short / medium /
  long term** horizons, ranked by conviction (how many strategies agree), ≤ 20 candidates
  each, **every idea with a stop-loss**. At the top, **🧺 Today's Basket** distils the whole
  ~30-idea board into ONE deployable best-of of **3–5 trades** — *"just tell me what to buy right
  now"* — sized as a single ¼-Kelly book (per-trade **weights summing to ≤100%**), greedily
  diversified (no two correlated names), regime-scaled, and **sticky day-to-day** so it's actually
  holdable. One click to **Track**, **paper-trade**, or **📈 Deploy as a NAV-tracked portfolio** (P&L,
  rebalance, AI review in the Portfolio tab), plus a **📊 Backtest** of the construction method
  (held-basket, monthly-rebalanced, point-in-time equity curve vs the benchmark — which found the
  method beats the S&P at a monthly cadence, +1.9% alpha / Sharpe 1.06). And a **🎯 My Hold Basket** —
  the committed, conviction-held counterpart to the churny daily view: built for staying power (conviction
  floor + longer-horizon tilt + wide hysteresis), frozen once committed, and it only asks you to change on
  a **material** event (a holding hit its stop/target, its thesis broke, or a dramatically-better idea
  appears) — a small reasoned diff you approve, alerted once. See
  [docs/curated-basket.md](docs/curated-basket.md). Includes an **Idea Tracker** that runs each idea as a
  trade — closing it on stop / target / expiry into **Past Trades** with a realized return, a
  realized win-rate scoreboard, and a cumulative idea-book equity curve. Click a candidate to
  deep-dive it in Analyze. A **🚀 Multi-Baggers** sub-tab runs a *fundamentals-first* screen over a
  broad mid/small-cap universe — high-growth, high-quality, smaller-cap compounders scored on six
  pillars (growth · quality · balance sheet · valuation · size/runway · ownership) with a bounded
  technical timing overlay, each card explaining *why it could multi-bag*. **📌 Track** a pick to
  follow its **bagger multiple** (current ÷ entry, milestones to 10×) + thesis drift over time in the
  Multi-Bagger Tracker. See [docs/multibaggers.md](docs/multibaggers.md).
- **Bots** — pick a bot + timeframe. **★ Intraday Trade Ideas** (the default) aggregates the whole
  bidirectional signal-bot family by conviction into ranked **long and short** idea books; the
  **signal bots** (breakout, mean-reversion, ema-trend, vwap-reclaim, multi-timeframe confluence)
  **scan** the crypto universe on 5m–4h for coins signaling long/short now (side / entry / stop /
  target / R), and **Edge ⚙** runs the triple-barrier backtest to show each bot's measured
  win-rate / expectancy (which then weights the idea conviction). The **Crypto Momentum Bot** runs
  its daily basket with an optional **AI risk review** and **Deploy** to a NAV-tracked portfolio.
- **Pipeline** — full run with live progress (WebSocket, polling fallback) → ranked
  results with composite + four sub-score bars, report links.
- **Analyze Stock** — single-ticker deep dive: composite gauge, sub-score bars,
  strengths/concerns, technical triggers, sentiment, risk flags; one-click report.
- **Reports** — generated markdown notes, rendered in-browser.
- **History** — persisted pipeline runs (SQLite), expandable to full results.

Key endpoints: `POST /api/v1/pipeline/run` (+ `WS /api/v1/pipeline/{id}/ws`),
`POST /api/v1/pipeline/run/sync`, `POST /api/v1/screen`,
`GET /api/v1/stocks/{ticker}`, `GET /api/v1/stocks/{ticker}/analyze`,
`GET /api/v1/reports/{file}`, `GET|POST /api/v1/screens`, `GET /api/v1/health`.

## Trading bots

Automatable strategies that turn the pipeline into a decision loop (`pipeline_core/bots/`),
sharing one registry ([`base.py`](pipeline_core/bots/base.py)).

### Intraday signal bots (crypto) — long **and** short

A family of per-coin **signal bots** that **scan the universe on a chosen timeframe**
(`5m / 10m / 15m / 30m / 1h / 4h` — Binance klines, `10m` resampled from `5m`). Every bot is
**bidirectional** — it looks for a long setup and its mirror-image short (crypto is freely
shortable) — and returns the coins signaling **long or short right now**, each with side +
entry / stop / target / reward-risk (R):

- **Breakout** (Donchian break up = long / down = short + volume), **Mean-Reversion** (RSI-2 /
  Bollinger oversold = long / overbought = short), **EMA-Trend** (EMA + ADX + DI, either way),
  **VWAP-Reclaim/Breakdown**, and the flagship
- **★ Multi-Timeframe Confluence** — long the pullback when the **higher-timeframe trend** *and*
  **BTC regime** are up; short the failed bounce when both are down. Trade with the bigger trend,
  enter on the smaller one.

Signals are computed on **closed candles only** (the forming bar is dropped) so they don't
*repaint* as the current bar develops.

**Intraday Trade Ideas** ([`bots/ideas.py`](pipeline_core/bots/ideas.py)) is the conviction layer
over the whole family — the intraday counterpart to the daily (backtest-driven) Trade Ideas. It
runs **every** signal bot on a timeframe and ranks coins by **conviction** (how many bots
independently agree on a direction), returning separate **long** and **short** idea books with
median entry / stop / target and the agreeing bots named.

**Backtested edge** ([`bots/backtest.py`](pipeline_core/bots/backtest.py)) — a **triple-barrier
walk-forward** that replays each bot over deep history on closed bars and follows every signal to
its **target / stop / time** exit, producing real per-bot × timeframe × **regime** (trend/range)
win-rate, expectancy, and profit-factor (net of costs). Conviction is then **weighted by measured
edge** instead of a raw vote count, so a low-edge bot's agreement no longer inflates a ranking
(graceful fallback to the count until a backtest is run). This is what turns "accuracy" from a
heuristic into a number.

```bash
curl "/api/v1/bots"                                        # registry (bots + timeframes)
curl "/api/v1/bots/ideas?timeframe=15m&min_conviction=2"   # ranked long + short ideas (all bots)
curl "/api/v1/bots/mtf-confluence/scan?timeframe=15m"      # one bot: coins signaling long/short now
curl -X POST "/api/v1/bots/backtest/run?timeframes=15m"    # measure the edge (background job)
curl "/api/v1/bots/backtest/edge"                          # win-rate / expectancy / PF per bot
```

MCP: `list_bots()` · `crypto_intraday_ideas(timeframe, universe, min_conviction)` ·
`scan_crypto_bot(bot_id, timeframe, universe, limit)` · `run_intraday_backtest(timeframes,
universe, bars)` · `get_intraday_bot_edge(timeframe, regime)`.

### Crypto Momentum Bot (daily basket)

[`crypto_momentum.py`](pipeline_core/bots/crypto_momentum.py) — a BTC-regime-gated,
volatility-targeted momentum basket:

1. **Regime gate** — only risk-on while BTC is above its 200-day MA, else all cash.
2. **Momentum rank** — top-N of the tradable universe (CoinGecko top-50, Binance OHLCV)
   by 30/90-day return.
3. **Inverse-vol sizing** — weights ∝ 1/volatility (capped per name), trailing stop.

**AI layer** ([`bots/ai.py`](pipeline_core/bots/ai.py)) — an optional LLM *risk manager* that
**reviews, never replaces** the deterministic basket: a plain-language briefing, a conviction
(proceed / caution / reduce), and per-name risk flags — reasoning only from the numbers (no
hallucinated news). It **degrades to a rules-only template** when no LLM is configured, and an
optional overlay (`apply_ai_risk`) can only **de-risk** — down-weighting flagged names to cash,
never adding exposure.

```bash
curl "/api/v1/bots/crypto-momentum?top_n=8&ai=true"   # basket + AI review (or all-cash)
curl -X POST "/api/v1/bots/crypto-momentum/deploy?ai=true"   # save as a NAV-tracked portfolio
```

MCP: `run_crypto_momentum_bot(top_n=8, ai=true, deploy=false)`. Schedule it (cron/routine) and
it runs as a live bot; the [portfolio NAV tracker](docs/portfolios.md) and
[trade ledger](docs/backtesting.md) record its realized track record — paper-trade first.

## Example

```python
from pipeline_core.agents.orchestrator import OrchestratorAgent
from pipeline_core.models.schemas import PipelineRequest, ScreenFilters

results = await OrchestratorAgent().screen_only(PipelineRequest(
    query="quality IT stocks, ROE above 15%, P/E under 25, low debt",  # needs LLM for NL
    filters=ScreenFilters(index="NIFTY 50", pe_ratio_max=25, roe_min=0.15),
    max_candidates=5,
))
for r in results:
    print(r.ticker, r.filter_match_pct, [ (e.filter_name, e.passed) for e in r.filter_explanations ])
```

## Filters (ScreenFilters)

Valuation: `pe_ratio_min/max`, `pb_ratio_max`, `ev_ebitda_max`, `peg_ratio_max`, `dividend_yield_min` ·
Growth: `revenue_growth_min`, `profit_growth_min`, `eps_growth_3y_min`, `sales_growth_3y_min` ·
Profitability: `roe_min`, `roce_min`, `opm_min`, `npm_min` ·
India-specific: `promoter_holding_min`, `promoter_pledge_max`, `delivery_pct_min` ·
Health: `debt_to_equity_max`, `interest_coverage_min`, `piotroski_score_min`, `fcf_yield_min` ·
Technical: `rsi_min/max`, `price_above_50dma/200dma`, `macd_bullish`, `near_52w_high/low_pct`, `volume_spike_min` ·
Universe: `index`, `sector`, `industry`, `market_cap_min/max_cr`.

## Tests

```bash
env/bin/python -m pytest -q     # 288 tests, network-free (all I/O mocked/fixtured)
```

## Configuration

All via `.env` / environment (pydantic-settings): `llm_provider`, `openai_api_key`,
`anthropic_api_key`, `ollama_base_url`, `llm_model`, cache TTLs, `default_index`,
`ingestion_concurrency`, `analysis_concurrency`, composite weights
(`weight_fundamental/technical/sentiment/risk`), `report_top_n`.

## Layout

```
pipeline_core/
  config.py            settings + paths
  models/schemas.py    ScreenFilters, StockScore, PipelineRun, …
  strategies.py        10 curated screening strategies (filter bundles)
  services/            market_data · nse_data · llm_service · run_store
  agents/              indicators · context · ingestion · screener · fundamental
                       technical · sentiment · risk · report_generator · orchestrator
  backtest/            Strategy Lab: strategies · engine · runner · data_loader
server.py              MCP server (FastMCP)
app.py                 FastAPI (REST + WebSocket)
static/                dashboard SPA (index.html · js/app.js · css/style.css)
docs/                  full documentation (start at docs/README.md)
tests/                 pytest suite (network-free)
data/                  cache/, reports/, backtests/, stocksense.db (created at runtime)
```
