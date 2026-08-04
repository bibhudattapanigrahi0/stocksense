# Architecture

StockSense is **one shared engine, two thin surfaces**. All logic lives in the
`pipeline_core` package; `app.py` (FastAPI) and `server.py` (FastMCP) are adapters that
call into it and translate results to their transport. No business logic is duplicated
between them.

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
  storage:  diskcache (TTL) · SQLite (runs, saved screens)

  pipeline_core/backtest/  ── Strategy Lab (parallel subsystem, same services)
```

## Package layout

```
pipeline_core/
  config.py              settings + paths (pydantic-settings)
  models/schemas.py      ScreenFilters, StockScore, PipelineRun, StockDetail, SavedScreen, enums
  strategies.py          10 curated *screening* strategies (filter bundles)
  utils.py               small helpers
  services/
    market_data.py       yfinance profiles/prices/statements + index constituents (diskcache)
    nse_data.py          NSE enrichment: promoter/pledge/FII/DII/delivery (scraping, best-effort)
    llm_service.py       OpenAI/Anthropic/Ollama abstraction + rules-only fallbacks
    run_store.py         SQLite persistence (runs, saved screens) via aiosqlite
  agents/
    indicators.py        pure technical-indicator math (see indicators.md)
    context.py           StockContext dataclass (shared per-stock data bundle)
    data_ingestion.py    universe + per-stock profile fetch (bounded concurrency)
    screener.py          staged deterministic filter registry + match scoring
    fundamental.py       health/valuation/moat scoring
    technical.py         indicator-driven BUY/HOLD/SELL scoring
    sentiment.py         news RSS → sentiment (LLM or lexicon)
    risk.py              volatility/leverage/pledge/liquidity risk
    report_generator.py  markdown research note (template-first, LLM polish)
    orchestrator.py      stage scheduling, fan-out/fan-in, progress, persistence
  backtest/              Strategy Lab — see backtesting.md
server.py                MCP server (FastMCP)
app.py                   FastAPI (REST + WebSocket) + static dashboard
static/                  vanilla-JS SPA (index.html, js/app.js, css/style.css)
tests/                   pytest suite (network-free)
```

## The agents

| Agent | Responsibility |
|---|---|
| **Orchestrator** | Runs the pipeline stages, fans out analysis concurrently, emits progress events, persists runs. Entry points: `execute()`, `screen_only()`, `analyze_ticker()`. |
| **Data Ingestion** | Resolves the universe (index constituents) and fetches per-stock profiles with bounded concurrency; assembles the `StockContext`. |
| **Screener** | Applies the staged filter registry deterministically and computes filter-match scores + per-filter explanations. Also `compute_technicals()` for snapshots. |
| **Fundamental** | Rule-based financial-health + valuation score; optional LLM "moat" narrative. |
| **Technical** | Computes indicators → a BUY/HOLD/SELL signal and a technical score. |
| **Sentiment** | Pulls Google-News RSS per ticker → sentiment score (LLM consensus, or a lexicon fallback when no LLM). |
| **Risk** | Volatility, leverage, promoter-pledge, and liquidity flags → a risk score and level. |
| **Report Generator** | Renders a markdown equity-research note. Template-first (always renders), LLM writes only the executive summary when configured. |

## The pipeline (8 stages)

1. **Intent** — NL query → `ScreenFilters` via schema-validated LLM structured output (one
   retry). No-op when no LLM is configured; explicit filters always take precedence.
2. **Universe** — resolve the index → constituents from the Nifty Indices CSV
   (NIFTY 50/100/200/500, NEXT 50, BANK, IT, MIDCAP 150, SMALLCAP 250, …), hardcoded
   NIFTY 50 fallback.
3. **Ingest** — batched profile fetch, `asyncio.Semaphore`-bounded
   (`ingestion_concurrency`, default 8). Per-stock failures are logged and skipped.
4. **Screen** — staged filter registry: cheap `info` filters across the whole universe
   first, then `nse` / `technical` / `quality` filters on survivors only
   (≤ 3× `max_candidates`). Missing data ⇒ the filter is **indeterminate** — excluded
   from scoring, never counted against the stock.
5. **Analyze** — for each candidate, one shared `StockContext` (profile + prices +
   statements + benchmark + news) feeds the four analysis agents **concurrently**.
6. **Fuse** — weighted composite (defaults `0.40/0.30/0.15/0.15`, risk inverted); weights
   are configurable and validated to sum to 1.0.
7. **Report** — markdown notes for the top-N picks (`report_top_n`).
8. **Persist** — every run (request, filters, candidates, scores, report paths, errors)
   is written to SQLite; history survives restarts.

## StockContext pattern

Analysis agents never re-fetch raw data. The ingestion stage assembles a `StockContext`
(`agents/context.py`) — profile, price history, financial statements, benchmark series,
news — once per stock and passes it to the fundamental/technical/sentiment/risk agents.
This removes duplicate network calls and makes every agent trivially unit-testable with a
fixture context.

## Services & storage

- **market_data** (yfinance) — profiles, price history, statements, index constituents.
  Blocking calls are wrapped in `asyncio.to_thread`; responses cached in **diskcache**
  with per-type TTLs.
- **nse_data** — India-specific enrichment by scraping NSE; best-effort (403/404s are
  common and handled by degrading the affected fields to optional/indeterminate).
- **llm_service** — provider abstraction with a rules-only fallback at every call site.
- **run_store** — SQLite via **aiosqlite**; connections are opened per operation (aiosqlite
  connections are event-loop-bound, so they are not shared across the app lifetime).
- **diskcache** — TTL cache at the service boundary (`data/cache/…`); the backtest history
  loader keeps its own long-lived store at `data/cache/backtest`.

## LLM policy

**Deterministic-first.** Filters, indicators, and scores are pure Python and unit-tested.
The LLM is used only for (a) NL intent parsing, (b) the fundamental "moat" and sentiment
narrative, and (c) the report's executive summary. With `llm_provider=none` (the default),
everything runs rules-only — a lexicon sentiment scorer and template summaries — so the
whole system works with **zero API keys**. Provider errors raise rather than leak
`[LLM Error]` text into outputs; callers decide the fallback.

## Two surfaces, shared helpers

Both surfaces call the same engine functions:

- **Screening/analysis:** `OrchestratorAgent.screen_only()` / `analyze_ticker()` /
  `execute()`.
- **Backtesting:** the REST endpoints and MCP tools both call the shared shaping helpers
  in `backtest/runner.py` — `leaderboard()`, `strategy_detail()`, `signals_summary()`,
  `strategy_library()` — so the leaderboard/sort/detail logic exists in exactly one place.

## Testing

`env/bin/python -m pytest -q` — **288 tests, all network-free** (I/O is mocked or served
from fixtures). Coverage includes:

- **Indicators** (`test_indicators.py`) — math vs reference values, including the expansion
  indicators (Supertrend, ADX, CCI, Williams %R, Keltner, ROC, OBV).
- **Backtest engine** (`test_backtest_engine.py`) — execution model: next-open entry, exit
  priority (stop > trailing > target > time > signal), open-position marking, metrics math.
- **Strategy library** (`test_strategies.py`) — every one of the 38 strategies is
  parametrized: unique ids, boolean/aligned signals, and coherent simulated trades.
- **Screening/scoring/fundamental/report** — filter registry, composite scoring, report
  rendering (incl. the concerns path), and LLM JSON parsing.

See [backtesting.md](backtesting.md) for the Strategy Lab and
[screening-and-analysis.md](screening-and-analysis.md) for the screening path in depth.
