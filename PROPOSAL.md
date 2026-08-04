# Stock Filter Analysis — Agentic Pipeline Proposal

**Status:** Draft for review
**Date:** 2026-07-19
**Existing implementation:** `/Users/bdpanigrahi/Documents/agy` (StockSense v1.0)
**Target project:** this repository (`stock-data`)

---

## 1. Executive Summary

Build an agentic pipeline that turns a natural-language or structured stock-screening
request into a ranked, multi-dimensional analysis of Indian equities (NSE/BSE), and
exposes it through two surfaces:

1. **REST + WebSocket API with a dashboard** (already partially built in `agy`), and
2. **an MCP server** (this project — `fastmcp` 2.11 and `mcp` 1.12.3 are already in the
   environment), so Claude and other MCP clients can invoke screening, analysis, and
   reporting as first-class tools.

A working v1 skeleton already exists in `agy`: 7 specialized agents, an orchestrator,
yfinance/NSE data services, an LLM abstraction (OpenAI/Anthropic/Ollama), Pydantic
schemas, and a FastAPI surface with WebSocket progress. This proposal recommends
**hardening and extending that codebase rather than rewriting it**, extracting a shared
core so both the HTTP app and the MCP server sit on the same engine.

---

## 2. Current State — Inventory of the `agy` Implementation

### 2.1 What exists and works

| Component | File(s) | State |
|---|---|---|
| FastAPI app + CORS + dashboard mount | `main.py` | Working |
| Pipeline REST + WebSocket progress | `routers/pipeline.py` | Working |
| Stock detail / search endpoints | `routers/stocks.py` | Working |
| Report download endpoint | `routers/reports.py` | Working (traversal-safe) |
| Orchestrator (intent → ingest → screen → parallel analyze → report) | `agents/orchestrator.py` | Working skeleton |
| Screener (25+ filter lambdas, technical pre-screen) | `agents/screener.py` | Working |
| Fundamental agent (rule-based health score + LLM moat) | `agents/fundamental.py` | Working |
| Technical agent (RSI/SMA/MACD/52w/volume) | `agents/technical.py` | Working |
| Sentiment agent (Google News RSS + LLM) | `agents/sentiment.py` | Working |
| Risk agent (volatility, leverage, pledge flags) | `agents/risk.py` | Working |
| Report generator (LLM markdown synthesis) | `agents/report_generator.py` | **Has a runtime bug** (§2.2) |
| yfinance service + diskcache | `services/market_data.py` | Working |
| NSE enrichment (promoter/FII/DII/delivery) | `services/nse_data.py` | Fragile by nature (scraping) |
| LLM provider abstraction | `services/llm_service.py` | Working, degrades gracefully |
| Rich filter/run schemas (India-specific fields) | `models/schemas.py` | Good foundation |

### 2.2 Known defects and gaps (to be fixed in Phase 0)

- **Runtime bug in report generator** — `agents/report_generator.py`:
  `concerns_list = "\n".join([f"- {c}" for s in fundamental.get("concerns", [])])`
  binds the loop variable `s` but interpolates `c` → `NameError` whenever a stock has
  any concerns. The report path is effectively untested.
- **Sequential data ingestion** — `orchestrator.py` loops `await fetch_stock_data(...)`
  per ticker. For NIFTY 500 this is ~500 serialized network calls; the slowest stage
  by far. Needs bounded-concurrency batch fetching.
- **Universe mismatch** — `config.py` defaults to `NIFTY 500`, but
  `market_data.get_index_tickers()` only handles NIFTY 50/100 and silently falls back
  to NIFTY 50. Broader indices need a real constituent source (NSE index CSV).
- **ROCE is approximated by ROE** — both the screener filter and `StockScore.roce`
  copy ROE. Either compute ROCE properly from financial statements or drop the field.
- **In-memory run state** — `active_runs` dict in `orchestrator.py` loses all history
  on restart and can't be shared across workers. Needs SQLite persistence (aiosqlite
  is already a dependency, unused).
- **Brittle LLM JSON parsing** — `llm_service.parse_natural_language_query()` strips
  markdown fences by string splitting; no schema validation or retry. Should use
  structured outputs / tool use with `ScreenFilters` as the schema.
- **Degraded reports on missing credentials** — the sample report on disk
  (`data/reports/INFY_*.md`) contains `[LLM Error: Missing credentials…]`. The
  pipeline should detect missing LLM config up front and either fail fast or run in an
  explicit "rules-only" mode.
- **Scoring weights hardcoded** — 40/30/15/15 composite in
  `orchestrator._analyze_single_stock()` with no rationale, no configurability, and
  no evaluation harness to validate them.
- **No tests, no evals, no CI.**
- **Analysis agents re-fetch data** the ingestion stage already pulled (each of
  fundamental/technical/risk calls `get_stock_info` / `get_price_history` again).
  Caching masks this, but the architecture should pass a shared context object.

---

## 3. Goals and Non-Goals

### Goals

1. **G1 — Trustworthy screening.** Correct, well-tested filtering over NIFTY 50/200/500
   universes with India-specific fundamentals (promoter holding/pledge, FII/DII trend,
   delivery %).
2. **G2 — Multi-agent deep analysis.** Parallel fundamental, technical, sentiment, and
   risk analysis per candidate, fused into an explainable composite score.
3. **G3 — Dual interface.** Same engine behind (a) the existing FastAPI/WebSocket
   dashboard and (b) an MCP server exposing tools to LLM clients.
4. **G4 — Speed.** NIFTY 500 screen end-to-end < 90 s warm cache / < 6 min cold;
   per-stock deep dive < 15 s.
5. **G5 — Reliability.** Runs persisted, resumable status queries, graceful per-stock
   failure isolation, rules-only fallback when no LLM key is configured.
6. **G6 — Evaluability.** A regression set of query→expected-filter parses and a
   backtest harness so score/weight changes are measurable, not vibes.

### Non-goals (v1)

- Live intraday/streaming analysis or trading execution of any kind.
- Portfolio construction, position sizing, or order management.
- BSE-only tickers and derivatives.
- Investment advice; output is decision-support research, with disclaimers.

---

## 4. Proposed Architecture

```
┌──────────────────────────── Interfaces ────────────────────────────┐
│  FastAPI dashboard (agy, exists)   │   MCP server (this project)   │
│  REST /api/v1/*  +  WS progress    │   FastMCP tools + resources   │
└───────────────┬────────────────────┴───────────────┬───────────────┘
                │                                    │
┌───────────────▼────────────────────────────────────▼───────────────┐
│                    PIPELINE CORE (shared package)                  │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Orchestrator                                                 │  │
│  │  intent parse → universe → ingest → screen → fan-out analyze │  │
│  │  → fuse & rank → report. Emits progress events.              │  │
│  └──────┬──────────┬──────────┬──────────┬──────────┬───────────┘  │
│         │          │          │          │          │              │
│  ┌──────▼───┐ ┌────▼─────┐ ┌──▼───────┐ ┌▼───────┐ ┌▼───────────┐  │
│  │ Data     │ │ Screener │ │ Funda-   │ │ Techn- │ │ Sentiment  │  │
│  │ Ingestion│ │ Agent    │ │ mental   │ │ ical   │ │ Agent      │  │
│  │ Agent    │ │          │ │ Agent    │ │ Agent  │ │            │  │
│  └──────┬───┘ └────┬─────┘ └────┬─────┘ └───┬────┘ └─────┬──────┘  │
│         │          │            │           │            │         │
│  ┌──────▼──────────▼────────────▼───────────▼────────────▼──────┐  │
│  │ Risk Agent  │  Report Generator  │  (new) Backtest Agent     │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  Services: market_data (yfinance) │ nse_data │ llm_service        │
│  Storage:   diskcache (TTL) │ SQLite (runs, screens, results)      │
└────────────────────────────────────────────────────────────────────┘
```

### 4.1 Agent roster

| Agent | Responsibility | v1 status | Planned changes |
|---|---|---|---|
| **Orchestrator** | Stage scheduling, fan-out/fan-in, progress events, run persistence | Exists | Async batch ingestion, SQLite run store, event bus for WS + MCP progress |
| **Data Ingestion** | Universe constituents, per-stock profiles, NSE enrichment | Exists | Real NIFTY 200/500 lists (NSE index CSV), bounded-concurrency batch fetch, shared `StockContext` passed downstream |
| **Screener** | Deterministic filter application + match scoring | Exists | Fix ROCE, declarative filter registry (replace lambda list), explainable per-filter pass/fail on results |
| **Fundamental** | Health score, valuation, moat (LLM) | Exists | Consume `StockContext`; compute real ROCE, Piotroski F-score from statements |
| **Technical** | Indicators → BUY/HOLD/SELL | Exists | Consume `StockContext`; add configurable indicator set |
| **Sentiment** | News RSS → LLM consensus | Exists | Cache per-ticker news; dedupe/similarity; rules-only fallback (lexicon) when no LLM |
| **Risk** | Volatility, beta, leverage, pledge, liquidity | Exists | Consume `StockContext`; NIFTY-relative beta |
| **Report Generator** | Markdown equity research note | Exists (buggy) | Fix NameError; template-first rendering with LLM polish pass (report renders even with LLM down) |
| **Backtest** *(new, Phase 3)* | Re-run filters/scoring on historical snapshots; hit-rate vs NIFTY | — | Validates weights before trusting them |

### 4.2 Pipeline stages

1. **Intent** — NL query → `ScreenFilters` via structured LLM output (schema-validated,
   one retry), merged over any explicit filters from the caller.
2. **Universe** — resolve index → constituents (NSE CSV → cached; hardcoded fallback).
3. **Ingest** — batch-fetch profiles with `asyncio.Semaphore` (e.g. 8–12 concurrent),
   diskcache TTLs (price 15 min, fundamentals 1 d, news 1 h). Per-stock failures are
   logged and skipped, never fatal.
4. **Screen** — deterministic filters first (cheap), technical filters second (needs
   price history, only on survivors). Returns ranked candidates with per-filter
   explanations.
5. **Analyze** — `asyncio.gather` over candidates × 4 analysis agents against the
   shared context; each agent returns score + evidence.
6. **Fuse & rank** — configurable weighted composite (default 40/30/15/15, risk
   inverted), persisted with the run.
7. **Report** — top-N (configurable, currently hardcoded top-1) markdown reports;
   template renders deterministically, LLM adds narrative when available.

---

## 5. Key Design Decisions

- **D1 — One core, two surfaces.** Extract `agy`'s `agents/`, `services/`, `models/`
  into an importable package (`pipeline_core`). FastAPI app and MCP server are thin
  adapters. No logic duplication between them.
- **D2 — Determinism before LLM.** Filters, indicators, and scoring are pure Python and
  unit-testable. LLMs are used only for (a) NL intent parsing, (b) qualitative
  narrative (moat, sentiment, report prose). Every LLM call has a rules-only fallback.
- **D3 — Concurrency via asyncio + semaphores**, not threads/processes. yfinance calls
  are blocking → wrap in `asyncio.to_thread`. Global rate budget configurable.
- **D4 — Cache at the service boundary** (diskcache, per-endpoint TTLs as in
  `config.py`). Analysis agents never fetch raw data directly in v2 — they receive a
  `StockContext`, which also makes them trivially unit-testable with fixtures.
- **D5 — Runs are data.** Every run (request, filters, candidates, scores, report
  paths, timings, errors) persists to SQLite. Enables history endpoints, MCP status
  tools, and the backtest harness.
- **D6 — Explainability.** Every score carries its evidence: filter pass/fail list,
  strengths/concerns, indicator values, risk flags. The MCP tools return structured
  JSON, not just prose.
- **D7 — Config over code** for weights, universes, TTLs, concurrency limits, and
  analysis depth (`quick` = rules only, `standard` = +moat/sentiment, `deep` = +peers
  and longer report).

---

## 6. Interfaces

### 6.1 REST / WebSocket (existing, retained)

- `POST /api/v1/pipeline/run` → `{run_id}`; `GET /api/v1/pipeline/{id}` → status/results
- `WS /api/v1/pipeline/{id}/ws` → progress events
- `GET /api/v1/stocks/{ticker}`, `GET /api/v1/stocks/search?q=`
- `GET /api/v1/reports/{file}.md`

### 6.2 MCP server (new — this project, FastMCP)

Tools (structured JSON in/out, all read-only):

| Tool | Description |
|---|---|
| `screen_stocks` | Structured `ScreenFilters` (and/or `query` string) → ranked candidate list with scores and explanations. Synchronous, bounded by `max_candidates`. |
| `analyze_stock` | Full 4-agent deep dive for one ticker → composite + sub-scores + evidence. |
| `get_stock_overview` | Fast fundamentals/technicals snapshot for one ticker (no LLM). |
| `run_pipeline` | Fire-and-forget full pipeline → `run_id` (for NIFTY 500-scale runs that exceed a tool-call timeout). |
| `get_pipeline_status` | Poll status/results of `run_id`. |
| `generate_report` | Markdown research note for a ticker (latest analysis or fresh). |
| `list_saved_screens` / `save_screen` | Named filter presets. |

Resources: `report://{run_id}/{ticker}` (markdown), `screen://{screen_id}` (preset JSON).

`server.py` becomes a thin FastMCP wrapper (~150 lines) over `pipeline_core`;
`test.ipynb` is the manual smoke-test notebook for the tools.

---

## 7. Repository Strategy

Recommended path — **evolve, don't rewrite**:

1. Keep `agy` as the app repo; fix defects there in place (Phase 0).
2. Extract `agents/`, `services/`, `models/` into `pipeline_core` (a local package,
   pip-installed editable into both projects initially; a shared repo later if needed).
3. This project (`stock-data`) hosts the MCP server and depends on `pipeline_core`.

Alternative considered and rejected: greenfield rewrite in `stock-data`. The `agy`
schemas, filters, and orchestration skeleton are sound; the defects are localized and
cheaper to fix than a rewrite, and a rewrite would stall the MCP surface (the actual
new value) behind weeks of re-implementation.

---

## 8. Roadmap

### Phase 0 — Stabilize the existing engine (~1 week)
- Fix report-generator `NameError`; add a test that renders a report with concerns.
- LLM health check at startup; explicit `rules-only` mode when unconfigured.
- Structured-output intent parsing with `ScreenFilters` validation + one retry.
- Fix config/universe mismatch; real NIFTY 200/500 constituent lists via NSE index CSV
  with hardcoded fallback.
- pytest skeleton: unit tests for every screener filter and the scoring function.
- **Exit criteria:** full NIFTY 50 run green end-to-end with and without an LLM key;
  tests pass.

### Phase 1 — Performance and persistence (~1 week)
- Semaphore-bounded batch ingestion; shared `StockContext` to analysis agents
  (removes duplicate fetches).
- SQLite run store (aiosqlite): runs, candidates, scores, reports; history endpoint.
- Real ROCE + Piotroski F-score from financial statements.
- **Exit criteria:** NIFTY 500 cold run < 6 min; run history survives restart.

### Phase 2 — MCP server in this project (~1 week)
- Extract `pipeline_core`; `agy` depends on it (no behavior change).
- FastMCP `server.py` with the §6.2 tools/resources; smoke-test notebook.
- **Exit criteria:** Claude Desktop (or `mcp` CLI) can `screen_stocks` and
  `analyze_stock` end-to-end; identical results to the REST path.

### Phase 3 — Evaluation and backtesting (~1–2 weeks)
- Golden set: ~30 NL queries → expected filters; parse-accuracy gate in CI.
- Backtest agent: historical filter runs, forward 3/6/12-month returns vs NIFTY;
  report hit-rate by weight configuration.
- Weight tuning from backtest results; document methodology + disclaimers.
- **Exit criteria:** composite weights justified by measured hit-rate, not defaults.

### Stretch (post-v1)
- Scheduled screens (cron) with diff alerts; earnings-calendar awareness;
  peer-relative valuation tables; PDF report export; auth + multi-user for the API.

---

## 9. Testing and Evaluation

- **Unit:** every filter lambda (→ registry), indicator math vs reference values,
  scoring/fusion, NSE/yfinance normalizers (recorded fixtures).
- **Contract:** `ScreenFilters` round-trip through the LLM parser (schema validation).
- **Integration:** canned-data pipeline run (no network) → expected ranked output;
  MCP tools invoked in-process over the same fixtures.
- **Eval harness:** NL-parse golden set; backtest hit-rate dashboard.
- **Manual:** `test.ipynb` smoke suite per release.

---

## 10. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| yfinance rate-limits / schema drift | Ingestion failures | diskcache TTLs, bounded concurrency, recorded fixtures for tests, per-stock failure isolation |
| NSE scraping blocked | Loss of India-specific fields | Graceful degradation (fields become optional), alternate sources (Trendlyne/Screener.in public pages) behind the same interface |
| LLM cost/latency in sentiment+moat per candidate | Slow deep runs | `analysis_depth` tiers, news caching, small model for parse/sentiment, rules-only mode |
| Scoring weights are arbitrary | Misleading rankings | Backtest harness (Phase 3) before any "signal quality" claims; scores presented as decision support, not advice |
| Blocking yfinance in async loop | Stalled pipeline | `asyncio.to_thread` wrappers, semaphores |
| SEBI/regulatory | Compliance | Read-only research tool, no execution; standard not-investment-advice disclaimers in every report |

---

## 11. Success Metrics

- NIFTY 500 screen: < 90 s warm / < 6 min cold; per-stock deep dive < 15 s.
- NL intent parse accuracy ≥ 90% on the golden set.
- ≥ 80% unit coverage on `pipeline_core` filtering/scoring modules.
- Zero crashing runs: all failures surface as per-stock or per-stage degradation.
- MCP tools return results identical to REST for the same inputs.
- Phase 3: documented backtest hit-rate for the default weight configuration.

---

## 12. Open Questions

1. **Constituent source of truth** — NSE index CSV (free, occasionally blocked) vs a
   paid feed? Proposal assumes NSE CSV + hardcoded fallback.
2. **Default LLM provider/model** — keep `gpt-4o-mini` default, or default to a local
   Ollama model for zero-cost runs? (Config already supports all three.)
3. **Where does `pipeline_core` live long-term** — editable local package, or its own
   repo once the API and MCP surfaces stabilize?
4. **Report depth** — is one markdown note per top pick enough, or should `deep` mode
   generate reports for all top-N candidates?
5. **Multi-user** — is the dashboard single-user/local for now (assumed yes)?
