# Screening & Analysis

This is the "which stocks pass these filters now, and how good are they" path — distinct
from the historical [backtesting](backtesting.md) path. It runs in two modes:

- **Screen-only** — deterministic filters → ranked candidates (fast, no LLM).
- **Full pipeline** — screen, then a four-agent deep dive fused into a composite score,
  then optional markdown reports.

## Filters (`ScreenFilters`)

A request carries a `ScreenFilters` object (and/or a natural-language `query` that an LLM
parses into one). Fields are grouped; all are optional and combine with AND semantics.

| Group | Fields |
|---|---|
| **Universe** | `index`, `sebi_category`, `sector`, `industry`, `market_cap_min_cr`, `market_cap_max_cr` |
| **Valuation** | `pe_ratio_min/max`, `pb_ratio_max`, `ev_ebitda_max`, `peg_ratio_max`, `dividend_yield_min` |
| **Growth** | `revenue_growth_min`, `profit_growth_min`, `eps_growth_3y_min`, `sales_growth_3y_min` |
| **Profitability** | `roe_min`, `roce_min`, `opm_min`, `npm_min` |
| **India-specific** | `promoter_holding_min`, `promoter_pledge_max`, `fii_holding_change_positive`, `dii_holding_change_positive`, `delivery_pct_min` |
| **Financial health** | `debt_to_equity_max`, `interest_coverage_min`, `piotroski_score_min` (0–9), `fcf_yield_min` |
| **Technical** | `rsi_min/max`, `price_above_50dma`, `price_above_200dma`, `macd_bullish`, `near_52w_high_pct`, `near_52w_low_pct`, `volume_spike_min` |

**Units convention:** ratios like growth/holding/margins are **decimals** (`0.15` = 15%,
`promoter_holding_min=0.5` = 50%). Market caps are in **INR crore**. See
[`models/schemas.py`](../pipeline_core/models/schemas.py) for exact field descriptions.

## Staged filter registry

The screener applies filters in cost order so expensive data is only fetched for survivors:

1. **`info`** — cheap profile filters (valuation, market cap, sector) across the **whole
   universe**.
2. **`nse`** — promoter/pledge/delivery filters (needs NSE enrichment).
3. **`technical`** — RSI / DMA / MACD / 52-week / volume (needs price history).
4. **`quality`** — ROCE / Piotroski / interest coverage / 3-year CAGR (needs statements).

Stages 2–4 run only on candidates that survive stage 1 (bounded to ≈ `3 × max_candidates`),
keeping NIFTY 500 screens fast.

### Indeterminate filters (explainability)

Each result carries a list of `FilterExplanation` records — `filter_name`, `passed`,
`actual_value`, and an **`indeterminate`** flag. When a data source is unavailable (e.g. an
NSE 403), the affected filter is marked *indeterminate* and **excluded from the match
count** rather than counted against the stock. `filter_match_pct` is computed only over
determinate filters, so missing India-specific data never silently fails a good company.

## The four analysis agents

For each surviving candidate the ingestion stage builds one `StockContext` (profile +
prices + statements + benchmark + news), then these run **concurrently**:

| Agent | Produces | Notes |
|---|---|---|
| **Fundamental** | `fundamental_score` (0–100) | Rule-based health + valuation; optional LLM "moat" narrative. |
| **Technical** | `technical_score` + `technical_signal` (BUY/HOLD/SELL) | From RSI, SMAs, MACD, 52-week position, volume. |
| **Sentiment** | `sentiment_score` | Google-News RSS → LLM consensus, or a lexicon fallback when no LLM. |
| **Risk** | `risk_score` + `risk_level` (Low/Med/High) | Volatility, leverage, promoter pledge, liquidity. |

Each score carries its evidence (strengths/concerns, indicator values, risk flags) in the
result's private analysis bundle, surfaced by `analyze_stock` / the Analyze tab.

## Composite scoring (fusion)

```
composite = weight_fundamental · fundamental
          + weight_technical   · technical
          + weight_sentiment   · sentiment
          + weight_risk        · (100 − risk)      # risk is inverted
```

Defaults are `0.40 / 0.30 / 0.15 / 0.15` and must sum to 1.0 (validated). Higher risk
lowers the composite. Weights are configurable — see
[configuration.md](configuration.md#composite-score-weights).

> The composite weights are a **default, not a validated signal**. The
> [Strategy Lab](backtesting.md) is the harness for measuring what actually worked
> historically.

## Analysis depth

`analysis_depth` tunes how much runs per candidate:

- **`quick`** — rules only (fastest, no LLM).
- **`standard`** — adds the LLM moat + news sentiment.
- **`deep`** — reserved for heavier future analysis.

## Output shape

Screening returns `StockScore` objects: identity + `filters_matched/total`,
`filter_match_pct`, `filter_explanations`, the four sub-scores + `composite_score`,
`technical_signal`, `risk_level`, and key metrics (P/E, P/B, ROE, ROCE, D/E, promoter
holding/pledge, dividend yield, RSI). See [api-reference.md](api-reference.md) and
[mcp-tools.md](mcp-tools.md) for how to invoke it.

## Curated screening strategies

Ten ready-made filter bundles ship in [`pipeline_core/strategies.py`](../pipeline_core/strategies.py)
(Quality Compounders, Value Hunters, GARP, Momentum Breakout, …). List them via
`GET /api/v1/strategies` or the `list_strategies` MCP tool, then feed a bundle's filters to
a screen/run. Full list in [strategies.md → Curated screening strategies](strategies.md#curated-screening-strategies).
