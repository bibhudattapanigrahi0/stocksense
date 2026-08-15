# Today's Basket — the curated cross-horizon best-of

> **UI note:** the *hero card* atop the Trade Ideas tab is now **🏆 Best Odds** — the day's fresh setups
> ranked purely by win probability (see [`basket.build_best_odds`](../pipeline_core/backtest/basket.py),
> REST `GET /api/v1/backtest/best-odds`, MCP `get_best_odds`) — a simpler "which trades are most likely
> to win" answer. Today's Basket (below) remains the balanced/sized/holdable **book**: it still rides the
> `/ideas` payload as `basket`, powers the Hold Basket + Coattail siblings and the deploy/backtest flows,
> and is served by `get_trade_basket`.

> **Sell-side sibling — the Coattail SHORT sleeve (🐋↓ Coattail Shorts).** The Coattail Basket keeps the
> longs the smart money is *accumulating*; its mirror keeps the **shorts the smart money is *distributing***
> (crypto whales net-short, via Hyperliquid top-trader flow). Built by
> [`basket.build_basket(distribute_mode=True)`](../pipeline_core/backtest/basket.py) — pools the board's
> SHORT candidates, keeps only names with `smart_money.direction == "distribute"`, and sizes them as one
> ¼-Kelly book with the regime scaling **inverted** (a risk-off tide is a *tailwind* for shorts). Rides the
> `/ideas` payload as `coattail_short_basket`; REST `GET /api/v1/backtest/coattail-short-basket`, MCP
> `get_coattail_short_basket`. Empty on long-only markets (US/IN/METALS) and in risk-on tides — honest by
> construction. Companion sell-side surfaces: a **distribution flag** on any idea (an ⚠ avoid pill on a
> long, a confirming pill on a short) and a **held-position exit nudge** in the tracker when the smart
> money turns against an open trade (both gated by `smart_money_distribution_enabled`).

The daily **Trade Ideas** board ([`backtest/runner.horizon_ideas`](../pipeline_core/backtest/runner.py))
surfaces up to **30 ideas** — 10 each for short / medium / long horizons, refreshed every day. That's
the right depth for *exploration*, but a real user can't take 30 trades: faced with 30 rows that
reshuffle daily, most people take none.

**Today's Basket** answers the simpler question — **"just tell me what to buy right now."** It distils
the whole board into ONE small, deployable best-of (3–5 trades) sized as a single book, and pins it to
the top of the Trade Ideas tab. It is the daily analog of the intraday
[shortlist](../pipeline_core/bots/selection.py), implemented in
[`pipeline_core/backtest/basket.py`](../pipeline_core/backtest/basket.py).

It is **complementary** to the full board, not a replacement — the board is the menu, the basket is the
recommended order. The 30-idea board still renders below it.

---

## How the basket is built

Everything is a pure function of numbers each idea already carries (post quality-gate + intra-bucket
de-dup), so it's network-free and unit-tested. Four steps:

### 1. Score — balanced / risk-adjusted

Each candidate gets a transparent composite (disclosed weights in `settings.basket_w_*`, surfaced via
`basket.basket_weights()`):

```
score = w_ev·EV + w_conf·confidence + w_quality·rank        (default 45 / 30 / 25)
```

| Term | What it is |
|------|------------|
| **EV** | Expected return %, `p·target − (1−p)·stop`, logistic-squashed. `p` is the idea's best win prob (learned-fusion → isotonic-calibrated → advertised, via [`sizing._win_prob`](../pipeline_core/sizing.py)). |
| **confidence** | Sample/agreement-aware trust. Daily ideas have no clean per-idea trade count, so this is strategy **agreement** (diminishing returns, mirroring [`selection.agreement_term`](../pipeline_core/bots/selection.py)), lifted when a calibrated/learned probability backs the win rate. The "balanced" lever — a thinly-agreed idea can't outrank a well-backed one on EV alone. |
| **rank** | The idea's existing `rank_score` (conviction × regime × soft factors), **normalized per-horizon** (÷ bucket max) so a 3-conviction long isn't unfairly beaten by a 5-conviction short. |

### 2. Select — greedy + diversified

Not just "top N". Pick the best, then repeatedly take the best remaining that is:

- **not in the same correlation cluster** as one already chosen (HARD — reuses the board-wide
  cross-horizon clusters from [`dedup.cluster_symbols`](../pipeline_core/dedup.py), so the basket is
  never four names that move as one);
- within a **soft per-sector cap** (`basket_sector_cap`, relaxed only if it can't otherwise fill);
- with a **soft horizon-spread** nudge (within a score tie-band, prefer an unrepresented horizon).

Names whose live price has already **spent the plan** — broken the stop or hit the target — are
excluded from selection (a bad fresh entry); the count is reported as `summary.excluded_spent`.

### 3. Size — one ¼-Kelly book

The chosen picks are sized together via [`sizing.size_book`](../pipeline_core/sizing.py), so each
`alloc_pct` is a real portfolio weight summing to **≤100%** (leaving cash if the book is light). The
**market regime** scales it: risk-on runs up to `basket_size_max` (5) names at the full risk budget;
risk-off shrinks to `basket_size_min` (3) and trims the budget (the same haircut the per-bucket board
applies in [`runner._size_bucket`](../pipeline_core/backtest/runner.py)).

### 4. Stick — hysteresis

A basket that reshuffles every day is one nobody can hold. Each still-valid prior member (present on
today's board, not stopped-out / at-target) carries a **score bonus**, so a challenger only displaces
it when it's *materially* better (beats the incumbent's raw score by more than
`basket_hysteresis_margin`). A broken incumbent gets no bonus and drops out naturally. The committed
basket is persisted per market as `data/backtests/basket_<MARKET>.json` (the hysteresis anchor),
advanced once per run/track — intra-day auto-refreshes stay stable against the same anchor.

---

## Using it

- **UI** — the 🧺 *Today's Basket* card at the top of the Trade Ideas tab. Each row shows its
  allocation **weight**, horizon, entry/stop/target, win% and expected return, and a 📝 paper button.
  The header gives the basket's expected return, blended win rate, exposure vs cash, diversification,
  and regime posture. **Track basket** commits + tracks all picks; **Paper-trade all** opens a paper
  trade for each at its weight.
- **REST** — `GET /api/v1/backtest/basket?market=IN` (also rides along on
  `GET /api/v1/backtest/ideas` as `basket`); `POST /api/v1/backtest/basket/track`.
- **MCP** — `get_trade_basket(market)` (*"just tell me what to buy right now"*) and
  `track_trade_basket(market)`.

---

## Tracking the basket — two ways

1. **As individual ideas** (*Track*) — each pick enters the idea ledger tagged `source="basket"` and is
   followed as its own trade (closes on stop / target / expiry). Good for per-idea outcomes.
2. **As a portfolio** (*Deploy as portfolio*) — the basket becomes a saved, NAV-tracked
   [`Portfolio`](../pipeline_core/portfolio/models.py) (one position per pick at its weight, cash = the
   basket's cash), so it's tracked as a **unit**: a NAV curve, P&L, rebalance, and AI review, all in the
   **Portfolios** tab. Implemented by [`runner.deploy_basket`](../pipeline_core/backtest/runner.py)
   (mirrors the crypto bot's `deploy`). **Idempotent + cost-basis-preserving** — re-deploying re-syncs
   the same portfolio and keeps entry prices on names still held, so the NAV curve survives rebalances.
   The live deployed book **re-syncs weekly** (a hook in the auto-refresh loop; `basket_autodeploy_enabled`).
   - **REST** `POST /api/v1/backtest/basket/deploy`; **MCP** `deploy_trade_basket(market)`.

## My Hold Basket — a committed book you can actually hold

Today's Basket is a **live view**, so it moves daily — fine for *discovery*, painful as a *commitment*.
**My Hold Basket** is the committed counterpart: you commit once, it's frozen and NAV-tracked, and it only
asks you to change on a **material** event — shown as a small reasoned diff you approve.

- **Built for staying power** — `build_basket(hold_mode=True)` applies a conviction floor
  (`hold_min_conviction`), a longer-horizon score tilt (`HOLD_HORIZON_FACTOR`), and a much wider
  hysteresis (`hold_hysteresis_margin` 0.30 vs the daily 0.10) → a smaller, higher-conviction, low-churn
  book.
- **Changes only on a material trigger** ([`hold_rebalance_plan`](../pipeline_core/backtest/basket.py)) —
  the anti-churn core, with a deliberately high bar:
  - **EXIT** — the holding hit its own stop/target, or its thesis broke (no live signal, or conviction
    fell below the floor).
  - **ADD** — only to refill a slot an EXIT just freed, or when a fresh name is *dramatically* higher
    conviction (`hold_displacement_conviction_gap`) → one bounded swap.
  - **HOLD** — everything else, *even if it's no longer today's #1* (this is the point).
- **Alerted, once** — a material change fires a fingerprinted `hold_rebalance` alert (via
  [`alerts.evaluate_market`](../pipeline_core/alerts.py)) — no daily nagging.
- **Commit / Apply** — Commit persists the book (entry prices + cost basis) and deploys it as a
  NAV-tracked portfolio; Apply adopts exactly the reviewed diff (held − exits + material adds), keeping
  cost-basis on survivors.
- **Surfaces** — REST `GET/POST/DELETE /api/v1/backtest/hold-basket[/commit|/apply]`; MCP
  `get_hold_basket` / `commit_hold_basket` / `apply_hold_basket`; the **🎯 My Hold Basket** card under
  Today's Basket (holdings + P&L + "✓ no action needed" or the reasoned diff with Apply).

## Backtesting the construction method

*Would this basket approach have made money?* [`basket_backtest.py`](../pipeline_core/backtest/basket_backtest.py)
runs a **held-basket, periodically-rebalanced** historical simulation with **anti-lookahead**: at each
rebalance the board is reconstructed *as of that date* (point-in-time signals / regime / clusters), the
same `build_basket` picks the basket, and its weights are held to the next rebalance. Daily returns
compound into an equity curve; turnover is charged at each rebalance; metrics reuse the engine's
`compute_metrics` (**CAGR / Sharpe / max-drawdown / alpha vs benchmark**), plus turnover and hit-rate.

**Rebalance cadence is the dominant lever** (real US backtest, 2019→2026):

| Cadence (`basket_rebalance_days`) | CAGR | Alpha vs S&P | Sharpe | Max DD |
|---|---|---|---|---|
| Weekly (5) | −5.3% | −20.3% | −0.28 | −46.7% |
| **Monthly (21) — default** | **+16.9%** | **+1.9%** | **1.06** | −22.0% |
| Quarterly (63) | +11.3% | −3.8% | 0.77 | −23.5% |

Per-rebalance turnover is ~119% at every cadence, but weekly does ~4× as many rebalances as monthly, so
it pays ~4× the cumulative cost — enough to turn a market-beating method into a loser. **Monthly is the
default** for this reason (turnover cost is ~linear in rebalance count, so this is mechanical, not a
period artifact). This is exactly what the backtest is *for*.

- **Anti-lookahead** is the point: `strategy.entry` is causal, so precomputing signals over full history
  and reading them as of date D is identical to computing on `df.loc[:D]`; regime and clusters use only
  data ≤ D; returns come from D+1 onward.
- **Scope boundary (deliberate):** this validates the *construction* method — conviction aggregation +
  cross-horizon diversification + regime-scaled ¼-Kelly + hysteresis. The live board's full-sample
  **calibration / fusion win-rate** layer is intentionally excluded (it would leak future outcomes into a
  past date), so the backtest ranks on conviction × regime × diversification. An expanding-window
  per-strategy win rate is a possible follow-on.
- **Reading it:** a stop-protected, partly-cash, rotating basket is a *different* risk profile from
  buy-and-hold — at the monthly default it beat the S&P with ~half the drawdown, but it's still one
  historical period on one market. Treat the result as a measurement of the method, not a promise.
- **Surfaces:** REST `POST …/basket/backtest/run` (background) + `…/status` + `GET …/basket/backtest`;
  MCP `run_basket_backtest(market, years, rebalance_days)` / `get_basket_backtest(market)`; a 📊 Backtest
  panel on the basket card (equity curve vs benchmark + metrics).

## Configuration ([`config.py`](../pipeline_core/config.py))

| Setting | Default | Meaning |
|---------|---------|---------|
| `basket_enabled` | `True` | Master switch. `False` leaves the board untouched (regression-safe). |
| `basket_size_min` / `basket_size_max` | `3` / `5` | Basket size in risk-off / risk-on (neutral = midpoint). |
| `basket_w_ev` / `basket_w_confidence` / `basket_w_quality` | `0.45` / `0.30` / `0.25` | Composite weights (normalized to sum to 1). |
| `basket_sector_cap` | `2` | Soft cap on names per sector (equity). |
| `basket_hysteresis_margin` | `0.10` | A challenger must beat an incumbent's score by more than this to swap it in. |
| `basket_autodeploy_enabled` | `True` | Weekly auto re-sync of an already-deployed basket portfolio. |
| `basket_redeploy_days` | `7` | Re-deploy an existing basket portfolio at most this often. |
| `basket_backtest_years` | `8` | History span for the basket backtest. |
| `basket_rebalance_days` | `21` | Rebalance cadence in trading days (~monthly — evidence-backed). |
| `basket_backtest_cost_bps` | `25.0` | Round-trip turnover cost per unit weight traded (bps). |
| `hold_min_conviction` | `2` | Hold Basket conviction floor (only well-agreed names). |
| `hold_hysteresis_margin` | `0.30` | Hold Basket incumbent stickiness (vs the daily 0.10). |
| `hold_displacement_conviction_gap` | `2` | A fresh name bumps a holding only if its conviction is ≥ this higher. |
| `hold_size_min` / `hold_size_max` | `3` / `5` | Hold-book size in risk-off / risk-on. |

> The expected-return figures are estimates from measured edge, not guarantees — decision support, not
> investment advice.
