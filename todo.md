# TODO

Backlog of ideas and follow-ups. Newest at the top.

## Portfolio performance tracking (mark-to-market over time)

**Status:** ✅ done (2026-07-27) — `pipeline_core/portfolio/track.py:snapshot_portfolio` +
`portfolio_snapshots` table + `POST /api/v1/portfolios/{id}/snapshot` / `GET …/history` +
auto-snapshot-on-load (deduped daily) + a NAV curve & per-position P&L column in the Portfolio
tab. The Trade Ideas tracker was likewise upgraded to a real ledger (stop/target/expiry →
past trades, realized win rate + idea-book equity curve). See docs/portfolios.md & docs/backtesting.md.

**Original note (kept for context):**
**Status:** idea / not started
**Why:** Saved portfolios are currently a *static snapshot* — one row in the `portfolios`
table with construction-time entry prices, weights, stops, and targets frozen at build time.
The only way to evaluate one against the market is the on-demand **Rebalance** action
([pipeline_core/portfolio/rebalance.py](pipeline_core/portfolio/rebalance.py)), which re-prices
held names vs their entry and shows drift — but it persists nothing. There is no NAV curve,
no P&L history, and no automatic tracking. (Contrast: the Trade Ideas tab *does* have a real
tracker — auto-snapshot + win/loss scoreboard.)

**Proposed v1:**
- New `portfolio_snapshots` table (or JSON store) keyed by portfolio id → time series of
  mark-to-market value + per-position price/return.
- Endpoint to record a snapshot (mark all positions to live price, compute portfolio return
  vs cost basis). Auto-snapshot on portfolio load, and/or on a schedule.
- Portfolio tab: a small **NAV / return curve** and a **P&L column** in the positions table.

**Building blocks that already exist (reuse, don't rebuild):**
- Live pricing: `runner._current_price` (used by rebalance's price marking).
- Proven tracker pattern to mirror: the Trade Ideas idea-tracker
  (`tracked_ideas.json`, auto-snapshot on each backtest, re-pricing scoreboard in
  [pipeline_core/backtest/runner.py](pipeline_core/backtest/runner.py)).
- Persistence layer: [pipeline_core/services/run_store.py](pipeline_core/services/run_store.py)
  (add a table alongside `portfolios`).
- Rebalance already marks held names to live price and computes `return_pct` vs entry —
  a snapshot is essentially "persist that, on a cadence."

**Note:** the "tracked" label in the Portfolio UI today means *stored + rebalanceable*, not
*monitored over time* — worth clarifying in copy once real tracking lands.
