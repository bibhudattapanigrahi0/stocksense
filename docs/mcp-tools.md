# MCP Tools Reference

Served by [`server.py`](../server.py) (FastMCP). All tools are read-only and return
structured JSON. Run:

```bash
env/bin/python server.py                     # stdio (Claude Desktop)
env/bin/python server.py --http --port 8001  # streamable HTTP
```

See [getting-started.md → Run the MCP server](getting-started.md#run-the-mcp-server) for the
Claude Desktop config. There are **22 tools** and **2 resources**.

---

## Screening & analysis

| Tool | Signature | Returns |
|---|---|---|
| `screen_stocks` | `(query?, filters?: ScreenFilters, max_candidates=10)` | Ranked candidates with per-filter pass/fail explanations + `llm_mode`. |
| `analyze_stock` | `(ticker, analysis_depth="standard")` | Four-agent composite (0–100) + sub-scores + evidence. Depth: `quick`/`standard`/`deep`. |
| `get_stock_overview` | `(ticker)` | Fast profile + NSE data + technicals (no LLM). |

## Pipeline (universe-scale jobs)

| Tool | Signature | Returns |
|---|---|---|
| `run_pipeline` | `(query?, filters?, analysis_depth="standard", max_candidates=10)` | Starts a background run → `{run_id}`. |
| `get_pipeline_status` | `(run_id)` | Progress + results for a run. |
| `list_pipeline_runs` | `(limit=10)` | Recent run history. |

## Reports

| Tool | Signature | Returns |
|---|---|---|
| `generate_report` | `(ticker)` | Markdown research note for one ticker. |

## Saved screens (presets)

| Tool | Signature |
|---|---|
| `save_screen` | `(name, filters: ScreenFilters, description?)` |
| `list_saved_screens` | `()` |
| `get_saved_screen` | `(name)` |
| `delete_saved_screen` | `(name)` |

## Index & curated strategies

| Tool | Signature | Returns |
|---|---|---|
| `get_index_constituents_list` | `(index="NIFTY 50")` | Current constituents (NIFTY 50/100/200/500, BANK, IT, …). |
| `list_strategies` | `()` | Curated **screening** strategies (filter bundles) — feed one to `screen_stocks`/`run_pipeline`. |

## Backtesting (Strategy Lab)

| Tool | Signature | Returns |
|---|---|---|
| `list_backtest_strategies` | `()` | Catalog of all 38 trading strategies (id/name/category/description). Works with no prior run. |
| `run_backtest` | `(index="NIFTY 500", years=10, cost_roundtrip_bps=25.0, strategy_ids?)` | Starts a **background** run. Poll `get_backtest_status`. Returns `already_running` if one is in flight. |
| `get_backtest_status` | `()` | Job state + last-run summary. |
| `get_backtest_leaderboard` | `(sort_by="sharpe")` | Ranked strategies with full metrics + BUY/SELL/HOLDING counts. Sort keys as in [api-reference.md](api-reference.md#backtesting). |
| `get_backtest_strategy` | `(strategy_id)` | One strategy's metrics + current BUY/SELL/HOLDING **stock lists** + best/worst trades + `plan` (stop/target/trail/time-stop rules for building trade ideas). Equity curve omitted (compact for LLMs). **This is how you find which stocks a strategy is buying/selling now.** |
| `get_backtest_signals` | `(signal_type?)` | Signals across **all** strategies; `signal_type` ∈ `buy`/`sell`/`holding` narrows each. |
| `get_trade_ideas` | `(max_per_bucket=20)` | Current buys grouped into **short / medium / long term** horizons, aggregated by stock and ranked by conviction (how many strategies agree). Each carries a **stop-loss** (`stop_price`/`stop_pct`) and a `side`. ≤ `max_per_bucket` per horizon. **CRYPTO trades both ways** — its board also surfaces backtested **SHORT** ideas (`side="short"`: stop above entry, target below, P&L inverted) beside the longs; every other market is long-only. Payload heads with the `regime` + `fear_greed` market-conditions blocks. **Answers "what should I buy (or short, on crypto) for a short/medium/long horizon now?"** |
| `get_market_conditions` | `(market='IN')` | The **Market Conditions** header — the risk-on/off `regime` (direction the board trades with) + the 0–100 `fear_greed` mood (US ⇒ real CNN index, CRYPTO ⇒ alternative.me, IN/other ⇒ a derived blend). **Answers "which way is the market leaning right now?"** |
| `get_best_odds` | `(market='IN', top_n=5)` | The **Best-Odds board** — the day's fresh setups ranked **purely by win probability** (best-available: learned → calibrated → advertised), de-duped to distinct names. Each item carries `win_prob` + `win_prob_source` (✓ when model/calibrated-backed) and the plan; `summary.win_prob_sources` tallies the mix. **Answers "which trades are most likely to win now?"** (vs `get_trade_basket`'s balanced, sized book). |
| `get_coattail_short_basket` | `(market='IN', max_per_bucket=10)` | The **sell-side coattail SHORT sleeve** — today's curated **SHORT** ideas the smart money is **distributing** (crypto whales net-short via Hyperliquid top traders). Same ¼-Kelly book sizing as the basket but **inverted** for the sell side (a risk-off tide is a tailwind). Empty on long-only markets (US/IN/METALS) and in risk-on tides — honest by construction. `summary.smart_money_backed` = how many shorts qualified. **Answers "what are the proven traders positioned SHORT on now?"** Also rides along on `get_trade_ideas` as `coattail_short_basket`. |
| `refresh_congress` | `(max_filings=250)` | Build/rebuild the **US congressional-trades** panel from keyless House Clerk PTR disclosures — a 2nd US smart-money actor family beside SEC 13F. **Heavy on first run** (bounded PDF fetches; per-filing cached → incremental after). Then congressional **buying *and* selling** rides along on the US board / coattail basket / 🏛 badge / `get_smart_money_leaders` (`congress` block). Returns tickers built + top buys. No-op without the `pypdf` dependency. **Answers "what is Congress trading now?"** |
| `get_tracked_ideas` | `()` | Follow-up scoreboard: every tracked idea (auto-snapshot each run) priced live → per-idea return % and status (`winning`/`losing`/`stopped_out`/`flat`) plus a **winning-vs-losing tally + win rate**. **Answers "how are our ideas doing?"** |

Typical flow: `run_backtest` → poll `get_backtest_status` until `running=false` →
`get_backtest_leaderboard` / `get_backtest_signals` / `get_backtest_strategy`.
Reading tools work against the existing `latest.json` without re-running. See
[backtesting.md](backtesting.md).

## Portfolios

Build and manage **model portfolios** — weighted, risk-shaped, theme-tilted baskets. See
[portfolios.md](portfolios.md).

| Tool | Signature | Returns |
|---|---|---|
| `list_portfolio_themes` | `()` | Curated theme catalog (slug, name, description, ticker count). |
| `list_risk_profiles` | `()` | The 3 risk presets + their constraints, plus weighting schemes and bases. |
| `construct_portfolio` | `(name, strategy_ids, basis="blend", theme?, risk="balanced", weighting="score", max_positions?, notes?, save=false)` | A constructed portfolio: positions with weights, per-name stops/targets, sector allocation, summary. `save=true` persists it. `basis` = `fundamental`/`systematic`/`blend` (systematic/blend need a prior `run_backtest`). |
| `list_portfolios` · `get_portfolio` | `()` · `(id_or_name)` | Retrieve saved portfolios. |
| `rebalance_portfolio` | `(id_or_name)` | Buy/trim/exit actions to move a saved portfolio to its fresh target, held names priced live. |
| `delete_portfolio` | `(id_or_name)` | Delete a saved portfolio. |

Model portfolios are decision-support research, not investment advice; metrics are weighted
constituent estimates, not a realized basket backtest.

## Health

| Tool | Returns |
|---|---|
| `health` | LLM mode, provider, default index, reports dir. |

---

## Resources

| URI | Description |
|---|---|
| `report://{filename}` | A generated markdown report (traversal-guarded, `.md` only). |
| `screen://{name}` | A saved screen preset as JSON. |

---

## Example prompts (Claude)

- *"Screen NIFTY 500 for quality stocks: ROE above 18%, D/E under 0.5, promoter holding over 50%."*
- *"Deep-dive INFY and tell me the risk flags."*
- *"Show the backtest leaderboard sorted by win rate."*
- *"Which stocks is the Connors RSI(2) strategy flagging as a buy right now?"* → `get_backtest_strategy("connors-rsi2")`.
- *"List all breakout strategies and their 10-year CAGR."*
