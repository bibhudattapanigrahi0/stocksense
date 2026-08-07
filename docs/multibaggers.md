# Multi-Bagger screen — fundamentals-first idea generation

The daily **Trade Ideas** board ([`backtest/runner.horizon_ideas`](../pipeline_core/backtest/runner.py))
is *technical*: it rolls the strategy backtest's current BUY signals into short/medium/long horizons
and answers **"what's a good entry right now?"**.

The **Multi-Bagger** sub-tab answers a different question — **"which businesses could compound many
times over?"** — the Peter Lynch "ten-bagger" idea: small, fast-growing, high-quality, well-owned
companies whose growth isn't fully priced in yet. It's a *fundamentals-first* screen, implemented in
[`pipeline_core/multibagger.py`](../pipeline_core/multibagger.py), and lives as a segmented toggle at
the top of the Trade Ideas tab (**⏱ By Horizon** | **🚀 Multi-Baggers**).

It is **complementary** to the horizon board, not a replacement — one is about *timing an entry*, the
other about *finding a long-horizon compounder*.

---

## The score

Each company gets a **0–100 multi-bagger score** = the weighted mean of six fundamental pillars, each
normalised to 0..1 from whatever metrics are actually present. Missing metrics are skipped and the
pillar re-normalises, so a name is **never flattered by absent data** (and a profile with too little
data — below `multibagger_min_coverage` of the six pillars — is dropped, not padded onto the board).

| Pillar | Weight | What it rewards | Metrics (info / **deep**) |
|---|---|---|---|
| **Growth** | 0.30 | fast, sustained top- & bottom-line growth | revenue/earnings growth · **3y sales/EPS CAGR** |
| **Quality** | 0.22 | profitable, capital-efficient compounders | ROE · debt · margins (`idea_ranking.quality_score`) · **Piotroski F** · **ROCE** |
| **Balance sheet** | 0.13 | low leverage, self-funding | debt/equity · free cash flow · **interest coverage** |
| **Valuation sanity** | 0.10 | growth not already fully priced | PEG (P/E fallback) |
| **Size / runway** | 0.15 | room to compound (the defining trait) | market cap (log-scaled per market) |
| **Ownership** | 0.10 | aligned skin in the game | insider % · **promoter holding & pledge (IN)** |

Weights live in `settings.multibagger_w_*` and are tunable / disclosed on the API payload.

### Technical overlay (soft, bounded)

On top of the fundamental base sits a small timing nudge (`multibagger_w_technical`, default 0.08 →
max ±8%): a name still **early in its 52-week range** — or one a strategy is **signalling a buy on
today** — gets a mild lift; an extended one gets trimmed. It's deliberately capped so it can **never
reorder the fundamental leaders** — it's *context on the card* (a `✓ buy signal now` chip, a
`% of 52w` chip), not a gate. Both inputs are free: the 52-week position comes from the same info
fetch, and the current-BUY set comes from the last persisted backtest.

Each candidate carries: `score`, the six `pillars` (0–100), `reasons` ("why it could multi-bag") and
`concerns` (what's weak/missing), `buy_signal_now`, `pct_in_52w_range`, and the raw `metrics`.

---

## Staged, cheap→expensive (like the Screener)

`screen_multibaggers(market, top_n)` runs in stages so a broad universe stays affordable:

1. **Universe** — the broad mid/small-cap hunting ground where multi-baggers form: **IN → NIFTY 500**,
   **US → S&P 500 ∪ NASDAQ-100** (deduped), capped at `multibagger_max_universe`.
2. **Info stage** — `market_data.fetch_batch_info` over the whole universe (one **1-day-cached**
   profile per name, bounded concurrency, progress → job state). Score every name; drop coverage
   failures; rank.
3. **Deep stage** — only the top `multibagger_deep_top_n` survivors pay for financial statements
   (`compute_quality_metrics` → Piotroski / ROCE / 3y CAGRs / interest coverage) plus, on IN,
   promoter holding & pledge. Per-name failures fall back to the info-only score.
4. **Overlay + persist** — apply the bounded technical overlay, sort, cap at `top_n`, and persist the
   ranked result to `data/backtests/multibaggers_<MARKET>.json`.

The tab **loads the persisted screen instantly**; **⟳ Refresh screen** recomputes in the background
behind a progress bar (mirrors the Strategy-Lab backtest job). Equity markets only — price-only
markets (metals / crypto) have no fundamentals and return an empty, note-bearing payload.

---

## Tracking

A **📊 Multi-Bagger Tracker** (top of the sub-tab) follows whether the picks actually compound. It's
deliberately *not* the horizon board's stop/target trade ledger — multi-baggers have **no stops or
targets**, so there's nothing to "close" them on. Instead each tracked name is followed by:

- **Bagger multiple** = `current ÷ entry_price`, with **milestones** at 1.5× / 2× / 3× / 5× / 10×
  (2× = a "double bagger"), plus return-since-entry.
- **Thesis drift** — the entry score vs a **fresh, deep-inclusive re-score** (statements re-fetched,
  1-day cached), so you see whether the fundamental case is strengthening (▲) or weakening (▼). The
  re-score is deep-inclusive *on purpose* — comparing a deep entry score to an info-only current score
  would read as false decay.
- A **basket scoreboard** (count, avg/best multiple, milestone tally) and an **unrealised equity
  curve** (equal-weight, each name normalised to its entry price, marked-to-market over time; hides
  gracefully when history is unavailable).

Tracking is **explicit** — 📌 Track a specific card, or ＋ Track all shown; unlike the horizon tracker,
a screen refresh never auto-tracks (these are hand-picked conviction bets). Picks are **open-ended
holds**, removed manually (🗑 / Reset). State persists per market to
`data/backtests/multibagger_tracked_<MARKET>.json`.

---

## API & MCP

| Surface | Call |
|---|---|
| REST | `GET /api/v1/backtest/multibaggers?market=&top_n=` — persisted screen (computes inline if none) |
| REST | `POST /api/v1/backtest/multibaggers/refresh?market=&top_n=` — background recompute (409 if running) |
| REST | `GET /api/v1/backtest/multibaggers/status?market=` — job progress + last-run summary |
| REST | `POST /api/v1/backtest/multibaggers/track?market=&ticker=&top_n=` — track one (ticker) or the top-N |
| REST | `GET /api/v1/backtest/multibaggers/tracked?market=` — tracked names re-priced (multiple / milestone / drift) + scoreboard + curve |
| REST | `DELETE /api/v1/backtest/multibaggers/tracked?market=&ticker=` — untrack one, or reset all |
| MCP | `get_multibagger_ideas(market, top_n)` — the ranked payload |
| MCP | `refresh_multibagger_ideas(market, top_n)` — recompute synchronously and return it |
| MCP | `track_multibagger_ideas(market, ticker)` — track one (or all current) → `{added, total}` |
| MCP | `get_tracked_multibaggers(market)` — tracked-status payload (multiples, milestones, drift, scoreboard) |

## Configuration (`pipeline_core/config.py`)

`multibagger_enabled`, `multibagger_max_universe` (500), `multibagger_top_n` (20),
`multibagger_deep_top_n` (25), `multibagger_min_coverage` (0.5), and the pillar weights
`multibagger_w_growth/quality/balance/value/size/ownership` + `multibagger_w_technical`.

## Caveats

A fundamentals-first *research* screen, not a prediction. Smaller companies are more volatile and can
fall as fast as they rise; a high score is a **starting point for your own due diligence**, not
investment advice. Fundamentals are point-in-time (yfinance / NSE, ~quarterly) and can be stale or
incomplete — the coverage gate and per-pillar renormalisation guard against acting on thin data, but
always confirm the numbers before trading. The technical overlay is *timing context*, not alpha.
