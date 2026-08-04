# Portfolio Management

StockSense can assemble and manage **model portfolios** — weighted, risk-shaped,
optionally theme-tilted baskets of Indian equities built from the strategies you already
have. One construction engine (`pipeline_core/portfolio/`) unifies both strategy libraries
behind a single recipe.

> ⚠️ Model portfolios are **decision-support research, not investment advice**. Weights are
> model targets; any metrics shown are weighted-average estimates from the constituent
> strategies, **not** a realized capital-constrained basket backtest. Systematic/blend
> baskets inherit the backtest's survivorship bias.

---

## The recipe: `PortfolioSpec`

A portfolio is defined by a spec and (re)constructed from it on demand:

| Field | Meaning |
|---|---|
| `name` | Portfolio name (unique when saved). |
| `basis` | `fundamental` · `systematic` · `blend` — which strategy library drives candidate selection. |
| `strategy_ids` | Strategy ids from the chosen basis (fundamental screen ids and/or trading strategy ids). |
| `theme` | Optional theme slug (e.g. `green-energy`) to tilt the universe. `null` = no tilt. |
| `risk` | `conservative` · `balanced` · `aggressive` — the risk-appetite preset. |
| `risk_overrides` | Optional partial overrides of the resolved risk profile (advanced). |
| `weighting` | `equal` · `score` · `conviction`. |
| `max_positions` | Optional override of the risk preset's position count. |

### Basis — where candidates come from

- **`fundamental`** — runs each selected **screening strategy**'s filters
  ([pipeline_core/strategies.py](../pipeline_core/strategies.py)) through the screener
  (`OrchestratorAgent.screen_only`) and ranks survivors by their filter/composite strength.
  Works any time (screens live data).
- **`systematic`** — pools the current **BUY signals** of the selected **trading strategies**
  ([pipeline_core/backtest/strategies.py](../pipeline_core/backtest/strategies.py)) from the
  latest backtest and aggregates **by stock** — more strategies flagging a name → higher
  conviction. **Requires a backtest first** (`run_backtest`).
- **`blend`** — does both and merges by ticker; a name flagged by *both* libraries gets a
  conviction boost, so cross-confirmed ideas rise to the top.

### Risk appetite → concrete constraints

Each preset resolves to a `RiskProfile` (see [risk.py](../pipeline_core/portfolio/risk.py)):

| | Universe | Max positions | Per-name cap | Sector cap | Cash | Fallback stop | Extra categories allowed |
|---|---|---|---|---|---|---|---|
| **conservative** | NIFTY 100 | 12 | 12% | 30% | 15% | 8% | Quality, Value, Income, Defensive, India-specific, Trend |
| **balanced** | NIFTY 200 | 15 | 10% | 35% | 8% | 10% | + Growth, Momentum, Breakout |
| **aggressive** | NIFTY 500 | 20 | 8% | 40% | 2% | 13% | + Contrarian, Mean Reversion, Volume |

`allowed_categories` gates both libraries' strategy `category` fields — a strategy whose
category isn't permitted at the chosen risk level is **skipped** during construction. Any
field can be overridden per-portfolio via `risk_overrides` (the `appetite` itself cannot).

### Themes — curated baskets

Themes are **hand-curated ticker lists** ([themes.py](../pipeline_core/portfolio/themes.py)),
because labels like "AI" or "green energy" don't map cleanly to GICS sectors. A theme tilt
keeps only candidates in the theme's basket (widened by a sector-keyword match where the
sector is known). Seed themes: `ai-it`, `green-energy`, `ev`, `defense`, `fintech-digital`,
`pharma-health`, `psu-banks`, `consumption`, `manufacturing`, `infra-realty`.

---

## Construction pipeline

`construct_portfolio(spec)` ([construct.py](../pipeline_core/portfolio/construct.py)):

1. **Resolve** risk profile, position count, universe index, theme set.
2. **Gather candidates** by basis (fundamental screen / systematic signals / blend).
3. **Theme filter** (if set).
4. **Score & rank**, trim to `max_positions`.
5. **Fill sectors** for systematic names (best-effort, cached).
6. **Size**: base weights by scheme → **per-name cap** → **sector cap** → scale to the cash
   target. Caps are ceilings honored when the opportunity set allows; with too few
   qualifying names, capital deployment wins and the relaxation is disclosed in
   `summary.notes`. Zero-weight tails are pruned.
7. **Attach** each position's stop (tightest strategy stop, else the risk fallback) and target.
8. **Summarize**: sector allocation, strategy coverage, concentration, and weighted-from-
   constituent metrics.

Each `PortfolioPosition` carries `weight`, `conviction`, `source_strategies`, `entry_price`,
`stop_pct`/`stop_price`, `target_pct`, and a short `rationale`.

---

## Rebalancing

`rebalance_portfolio(portfolio)` ([rebalance.py](../pipeline_core/portfolio/rebalance.py))
reconstructs the fresh target from the stored spec and diffs it against the held positions:

| Action | When |
|---|---|
| **BUY** | In the fresh target, not currently held. |
| **EXIT** | Held, no longer selected. |
| **ADD** / **TRIM** | Held, target weight drifted beyond the ±2% band. |
| **HOLD** | Held, within the band. |

Held names are marked against the latest price for an unrealized return.

---

## Persistence

Portfolios are stored in the `portfolios` table (SQLite, alongside runs and saved screens —
[run_store.py](../pipeline_core/services/run_store.py)) as a JSON payload of the full
`Portfolio` (spec + positions + summary). `save/list/get/delete_portfolio` mirror the saved-
screen helpers.

---

## REST API

| Method & path | Purpose |
|---|---|
| `GET /api/v1/portfolios/config` | Risk presets, weighting schemes, basis options. |
| `GET /api/v1/portfolios/themes` | Curated theme catalog. |
| `POST /api/v1/portfolios/construct` | Construct from a `PortfolioSpec` → portfolio (preview, not saved). |
| `POST /api/v1/portfolios` | Construct **and save** (201). |
| `GET /api/v1/portfolios` | List saved portfolios. |
| `GET /api/v1/portfolios/{id}` | One saved portfolio (id or name). |
| `POST /api/v1/portfolios/{id}/rebalance` | Buy/trim/exit actions to reach the fresh target. |
| `DELETE /api/v1/portfolios/{id}` | Delete a saved portfolio. |

```bash
# A balanced, green-energy portfolio from a fundamental quality screen (preview)
curl -s localhost:8000/api/v1/portfolios/construct -H 'content-type: application/json' -d '{
  "name": "Green Quality",
  "basis": "fundamental",
  "strategy_ids": ["quality-compounders", "dividend-stalwarts"],
  "theme": "green-energy",
  "risk": "balanced",
  "weighting": "score"
}'
```

Systematic/blend baskets need a backtest first (`POST /api/v1/backtest/run`); without one
they return `400` with a "run a backtest first" message.

---

## MCP tools

| Tool | Purpose |
|---|---|
| `list_portfolio_themes` | Curated theme catalog. |
| `list_risk_profiles` | Risk presets + weighting schemes + bases. |
| `construct_portfolio` | Build a portfolio from a recipe; `save=true` persists it. |
| `list_portfolios` · `get_portfolio` | Retrieve saved portfolios. |
| `snapshot_portfolio` | Mark to live price and record a NAV point (see below). |
| `rebalance_portfolio` | Buy/trim/exit actions for a saved portfolio. |
| `delete_portfolio` | Remove a saved portfolio (cascades its snapshots). |

---

## Performance tracking (NAV over time)

A saved portfolio is a static snapshot (entry prices frozen at construction). To track it,
`pipeline_core/portfolio/track.py:snapshot_portfolio` marks every position to its live price —
market-aware via `spec.market` — computes each name's return vs entry and a weight-weighted
portfolio return (cash + unpriced names count as flat), and records it as a NAV point
(`nav = 1.0` at cost basis) in the `portfolio_snapshots` table.

- **Auto-snapshot** on `GET /api/v1/portfolios/{id}` load, deduped to once per calendar day; the
  response carries `latest_snapshot`.
- **Manual**: `POST /api/v1/portfolios/{id}/snapshot` (dashboard **📸 Snapshot** button) or the
  `snapshot_portfolio` MCP tool.
- **History**: `GET /api/v1/portfolios/{id}/history` → the NAV time series, rendered as a NAV
  curve in the Portfolio tab, with a per-position **P&L** column.

Example prompts:
- *"Build me an aggressive AI-themed portfolio from momentum and breakout strategies and save it."*
- *"What should I rebalance in my Green Quality portfolio?"*

---

## Limits & honesty

- **Not a basket backtest.** Summary metrics are weighted averages of constituent-strategy
  metrics — a true held-basket equity curve (and inverse-vol weighting) is the next phase.
- **Survivorship bias** flows through from the systematic backtest (current index members only).
- **Theme baskets are curated** and need periodic revalidation against live NIFTY 500
  membership (symbols drift, e.g. ZOMATO→ETERNAL).
- **Sector caps are best-effort** for systematic names (sector fetched live) and relax for
  deliberately single-sector themed baskets — always disclosed in `summary.notes`.
