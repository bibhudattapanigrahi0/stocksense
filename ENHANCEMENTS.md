# StockSense Enhancements — Data Freshness & Strategy Backtesting

**Status:** Proposal for review
**Date:** 2026-07-25
**Builds on:** PROPOSAL.md (v2 implemented), extends its Phase 3

---

## Part 1 — Data Freshness

### 1.1 Current state and its problems

All caching is **lazy, pull-based, and TTL-only** (`diskcache` in `services/`):

| Data | Current TTL | Problem |
|---|---|---|
| Price history | 15 min, fixed | Same staleness at 10:00 IST (market live) and midnight. Intraday screens can be 15 min behind a moving market |
| Fundamentals/profile | 24 h | Stale right after an earnings release — exactly when accuracy matters most |
| News | 1 h | Acceptable, but no awareness of market hours |
| NSE holdings/delivery | 24 h | And it fails silently often — user can't tell *which* data is missing or how old |
| Index constituents | 24 h | Fine |

Structural gaps:

1. **No staleness visibility.** Cache entries are raw values — no `fetched_at`. The UI/report never says "prices as of 14:32, 12 min old". Users can't judge trust.
2. **No market-hours awareness.** NSE trades 09:15–15:30 IST. Freshness needs differ wildly inside vs. outside that window.
3. **Cold-start latency.** NIFTY 500 screen takes minutes on an empty cache because warming is lazy. Nothing prefetches.
4. **Silent source degradation.** NSE 403/404s (observed live) leave fields empty with no health signal anywhere.
5. **No invalidation control.** Only TTL expiry — no way to force-refresh a ticker after news/earnings.

### 1.2 Proposed enhancements

#### F1 — Freshness metadata wrapper (foundation, do first)
Wrap every cache entry: `{value, fetched_at, source, ttl}`. A small `CachedStore` helper in `pipeline_core/services/cache.py` replaces bare `diskcache` gets/sets in `market_data`, `nse_data`, `sentiment`.
- API responses gain `data_as_of` blocks (e.g. `{"prices": "2026-07-25T14:32+05:30", "fundamentals": "…"}`).
- UI shows staleness badges on cards: green <15 min, amber <24 h, red older; the report header prints a data-as-of line.
- **Effort:** ~1 day. Everything else builds on it.

#### F2 — Market-hours-aware TTL policy
Per-type TTL functions instead of constants:
- **Prices:** 2 min during 09:15–15:30 IST weekdays, 6 h otherwise (nothing moves when closed; weekly candles re-fetched after Friday close).
- **News:** 15 min market hours, 2 h otherwise.
- **Fundamentals:** 24 h, but see F5 earnings override.
- **NSE delivery %:** refresh after ~18:30 IST when NSE publishes; cached through the next morning.
Implement as `ttl_for(kind, now_ist)` in the cache layer; zero agent changes.

#### F3 — Scheduled warming (kills cold starts)
An asyncio scheduler started with the app (opt-in, `warm_enabled`):
- **08:45 IST weekdays:** refresh constituents + full profiles for the default universe (and each saved screen's universe) → first user screen of the day is always warm.
- **Every 15 min during market hours:** re-warm hot tickers only (recently analyzed, in active runs, in saved presets).
- **After close:** refresh the day’s OHLCV for the universe (final candles) and delivery %.
Bounded by the same ingestion semaphore; failures degrade to lazy mode. **Effort:** ~1–2 days.

#### F4 — Source health monitoring + stale-while-revalidate
- Per-source stats in the cache layer: successes, failures, last_error, last_success_at, avg latency.
- `GET /api/v1/health/sources` → dashboard health strip (Yahoo / NSE / NiftyIndices / News / LLM dots).
- **Stale-while-revalidate:** within 2×TTL, serve the stale entry instantly and refresh in the background; beyond that, fetch synchronously. Turns NSE flakiness from "empty fields" into "slightly old fields".
- **Effort:** ~1 day.

#### F5 — Corporate calendar awareness
- Pull earnings dates from `ticker.calendar` (yfinance) for universe stocks; persist per-ticker `next_earnings`.
- Auto-invalidate fundamentals on/after results day; surface "Results on Jul 28" chips in the UI; optionally flag screens run within ±2 days of earnings ("volatility window") in reports.
- **Effort:** ~1 day.

#### F6 — Explicit refresh controls
- `force_refresh: bool = false` param on screen/analyze endpoints and MCP tools; per-ticker `POST /api/v1/cache/invalidate/{ticker}`; a ⟳ button in the UI header that invalidates + reruns the current view.
- **Effort:** ~½ day.

#### F7 — Intraday bars for momentum (optional)
5m/15m interval history for technical-stage filters during market hours (TTL 1–5 min). Enables true intraday momentum strategies later (Part 2). Only for candidates (survivors), never the universe. **Effort:** ~½ day.

#### F8 — Fallback sources (resilience, later)
Promoter/pledge fallback chain: NSE API → screener.in public page → BSE corporate filing page; quote fallback: yfinance → NSE quote. Same `CachedStore` interface, `source` field records which one served. **Effort:** 2–3 days, only if NSE reliability stays poor.

### 1.3 Priority and sequencing

| Order | Item | Why first |
|---|---|---|
| 1 | F1 freshness metadata | Foundation; everything reads it |
| 2 | F4 health monitoring | Makes current silent failures visible immediately |
| 3 | F2 market-hours TTLs | Cheap, big correctness win |
| 4 | F3 scheduled warming | Removes the worst UX (cold NIFTY 500) |
| 5 | F6 refresh controls | Cheap, user-facing |
| 6 | F5 earnings awareness | High value, needs F1 |
| 7 | F7/F8 | Optional / as-needed |

**Success metrics:** screen responses carry `data_as_of` 100% of the time; warm-cache hit rate ≥ 90% during market hours; source failures visible in UI within one run; zero cold-start runs > 60 s for saved universes.

---

## Part 2 — Strategy Generation & Backtesting

### 2.1 The honest framing first

Today's screens are **cross-sectional and point-in-time**: "which stocks pass these filters *now*". Backtesting asks "what if I'd run this every month for 5 years and held the survivors?". Two data realities shape the design:

- **Price/volume history is fully backtestable** — OHLCV for any date range (technicals, momentum, breakouts).
- **Fundamentals are partially backtestable** — yfinance gives ~4 years of annual statements (so ROCE, Piotroski, growth, D/E can be recomputed per year), and price-derived ratios at time *t* are reconstructible: `P/E_t = price_t / EPS_ttm`, `Mcap_t = price_t × shares`, dividend yield from dividends history. **NSE promoter/delivery/FII-DII have no history → excluded from backtests** (flagged honestly in results).
- **Institutional holdings are partially backtestable (US)** — since added, **SEC EDGAR 13F** filings give a *real quarterly history* of large managers' US holdings (~2013+, filed with a ~45-day lag). `pipeline_core/services/edgar_13f.py` aggregates a curated ~30-manager basket into a per-ticker series keyed by filing date (anti-lookahead); it powers the `institutional-13f-accumulation` backtest strategy and the richer US ownership badge. Still coarse (quarterly, US-only, a basket ≠ total float) — see [docs/institutional-data.md](docs/institutional-data.md). The IN-side FII/DII history remains unavailable.
- **Biases to declare, not hide:** survivorship (we test today's constituents — past members that crashed aren't in the universe), no delisting data, annual-statement granularity for fundamentals. Every backtest output carries a bias disclaimer block.

### 2.2 What we're building

```
┌───────────────────────── STRATEGY SPEC ─────────────────────────┐
│  StrategySpec (pydantic):                                       │
│   universe, entry_rules (filters/signals), exit_rules,          │
│   sizing (equal/score-weighted), max_positions,                 │
│   rebalance (weekly/monthly/quarterly/annual),                  │
│   costs (brokerage, STT, slippage_bps), benchmark               │
└───────┬───────────────────────────────┬─────────────────────────┘
        ▼                               ▼
┌───────────────┐             ┌──────────────────────┐
│  GENERATION    │             │  BACKTEST ENGINE      │
│  - templates   │  specs →    │  - signal replay      │
│  - param sweep │             │  - portfolio sim      │
│  - NL → spec   │             │  - India cost model   │
│  - random/genetic│           │  - metrics + tearsheet│
└───────────────┘             └──────────┬───────────┘
                                         ▼
                          equity curve · drawdown · trades ·
                          CAGR/Sharpe/Sortino/maxDD/Calmar ·
                          alpha/beta vs NIFTY · hit rate
```

### 2.3 Backtest engine (`pipeline_core/backtest/`)

**Data layer** (`data_loader.py`)
- Long OHLCV loader: up to 10 y daily for universe + benchmark. Historical bars are **immutable** → cache permanently keyed `(symbol, interval)` with incremental append (only fetch the tail). This is what makes sweeps fast.
- Statement alignment: annual statements indexed by fiscal year; TTM EPS/book value reconstructed per rebalance date with a **90-day reporting lag** (you can't use March results before ~June) — the anti-lookahead rule.

**Signal replay** (`signals.py`)
- Reuses the existing `ScreenerAgent` registry where possible: technical filters are pure functions of price history (recompute per date window), quality filters recompute per fiscal year.
- New time-series rules for trading strategies: RSI thresholds, DMA crossovers, trailing stops, take-profit, max-holding-days.

**Portfolio simulation** (`engine.py`) — vectorized pandas, daily marks:
- Entry at next open after signal (no same-close execution — no lookahead).
- Equal-weight or score-weighted, `max_positions`, rebalance cadence, delisting/dropout = exit at last price.
- **India cost model:** brokerage (flat ₹20 or 0.03%), STT 0.1% sell side, exchange+GST ≈ 0.0001, slippage bps (default 10 bps liquid large-cap, 30 bps otherwise) — configurable per run.
- Outputs: daily equity curve, drawdown series, trade log, exposure/turnover stats.

**Metrics** (`metrics.py`)
- CAGR, volatility, Sharpe, Sortino, max drawdown, Calmar, hit rate, avg win/loss, profit factor, alpha/beta vs NIFTY 50, rolling 1y returns.

### 2.4 Strategy generation

1. **Template library** — parameterized families covering the classics:
   - *Momentum*: top-N by k-month return, above 200-DMA, monthly rebalance.
   - *Mean reversion*: RSI < X in uptrend, exit RSI > Y or Z days.
   - *Breakout*: within X% of 52w high + volume spike, trailing stop.
   - *Quality/Value annual*: Piotroski ≥ F, ROCE ≥ r, P/E ≤ p — annual rebalance on statement years (the only honest cadence for fundamentals).
2. **Parameter sweep** — grid or Latin-hypercube over each template's knobs (e.g. RSI ∈ {25,30,35}, holding ∈ {5,10,20} days). Hundreds of vectorized runs are cheap once history is cached.
3. **NL → StrategySpec** — extend the existing intent parser: "buy oversold quality stocks, hold 2 weeks" → validated `StrategySpec` JSON. LLM optional; templates cover no-LLM mode.
4. **Overfit guards (non-negotiable):**
   - Train/test split (e.g. 2016–2021 select, 2022+ validate) and **walk-forward** windows;
   - Rank by validation Sharpe, never train Sharpe;
   - Sensitivity table: metrics across ±20% parameter perturbations — a strategy that only works at RSI=30 exactly is curve-fit;
   - Deflated expectations: report how many variants were tried (multiple-comparison reality check).

### 2.5 API & UI

- `POST /api/v1/backtest` (spec) → run_id; `GET /api/v1/backtest/{id}` → metrics + curves (JSON); `POST /api/v1/backtest/sweep` (template + grid) → ranked leaderboard; results persisted in a new `backtests` SQLite table.
- MCP tools: `run_backtest`, `get_backtest_result`, `sweep_strategy`.
- **New "Backtest" dashboard tab:** strategy picker (from Strategies tab templates or custom spec), param editor, run → equity curve vs NIFTY (Chart.js CDN, like marked.js), drawdown chart, metrics table, trade log, sweep leaderboard with validation columns. Bias disclaimer rendered on every result.

### 2.6 Phasing & effort

| Phase | Scope | Effort |
|---|---|---|
| B0 | F1/F4 freshness (prereq for trustworthy runs) | 2 d |
| B1 | Data layer: long-history loader, immutable cache, statement lag alignment | 2 d |
| B2 | Engine: technical strategies only (fully price-driven, zero fundamentals ambiguity) + costs + metrics | 3 d |
| B3 | Templates + parameter sweep + leaderboard | 2 d |
| B4 | API/MCP/UI tab with charts | 2 d |
| B5 | Fundamental-factor annual backtests (Piotroski/ROCE/PE_t) + walk-forward + sensitivity | 3 d |
| B6 | NL → StrategySpec; genetic search (optional) | 2 d |

**~2.5 weeks to B4 (usable backtesting for technical strategies), ~4 weeks complete.**

### 2.7 Risks & mitigations

| Risk | Mitigation |
|---|---|
| Lookahead bias | Execution at next open; 90-day statement lag; code-reviewed `signals.py` with unit tests asserting no future data |
| Survivorship bias | Declared on every output; optional NIFTY 500 + delisted-proxy sensitivity note |
| Overfitting | Train/validation split, walk-forward, sensitivity tables, variant-count disclosure |
| Cost understatement | Default 10–30 bps slippage + full India charge stack; stress test at 2× costs |
| yfinance history gaps/adjustments | Adjusted-close normalization check; per-symbol data-quality flag in results |
| Sweep compute | Immutable history cache + vectorized engine; sweep jobs run as background pipeline tasks with progress |

### 2.8 Success metrics

- 10-year, 500-stock momentum backtest completes < 30 s warm.
- Every result ships: metrics, equity/drawdown curves, trade log, cost breakdown, bias disclaimers — or it doesn't ship.
- Sweep leaderboard always separates train vs. validation metrics.
- Backtest of the existing curated strategies (Part: Strategies tab) produces a documented hit-rate table per strategy — finally validating the composite weights with data (the PROPOSAL.md Phase-3 exit criterion).

---

## Recommended immediate next step

**F1 + F4 (freshness metadata + source health)** this week — they're prerequisites for trusting backtest data — then **B1/B2** to get the first real equity curves on the board for the price-driven strategies (Momentum Breakout, Oversold Quality Reversal) already in the Strategies tab.
