# Trade-Idea Efficiency — Audit & Improvement Roadmap

> **Status (2026-08):** **Phase 1 SHIPPED** — rank-efficiency north-star metric (1.3), realistic
> per-market cost + crypto short funding (1.2), a documented survivorship win-rate haircut (1.1, the
> honest interim before the point-in-time universe 3B), and the crypto-shorts *watchlist-until-proven*
> gate (3.3, pulled forward). Config: `rank_ic_min_samples`, `cost_bps_*`, `crypto_funding_bps_per_day`,
> `survivorship_haircut_pp`, `crypto_shorts_actionable`. Tests: `tests/test_idea_efficiency.py`. Next:
> **Phase 2** (seed the ledger via replay → meta-labeling / attribution-weights / OOS gate), gated on
> real rank-IC numbers accumulating.


*Grounded in a read-through of the live idea pipeline (engine → strategies → `horizon_ideas` →
enrich/rank → tracker) as of 2026-08. "Efficiency" here = **realized edge per surfaced idea** and the
**accuracy of the ranking** — i.e. closing the gap between what the backtest promises and what the
board actually delivers.*

## The core problem, in one sentence

The board is optimized on backtests that are **survivorship-biased, flat-costed, and full-sample
(in-sample-ish)**, while the three layers designed to correct that — win-rate **calibration**, the
**fusion** win-model, and **factor attribution** → weights — are all **cold-started or not wired
back**. So today the board ranks largely on *raw, optimistic* backtest win-rates, and nothing yet
measures whether a **rank-1 idea actually beats a rank-10 idea**.

Everything below is ordered by *impact ÷ effort*. Tier 1 is the honest foundation; Tier 2 makes the
existing ML layers earn their keep; Tier 3 is structural upside.

---

## Tier 1 — Realism & the north-star metric (do first; cheap, high trust)

### 1.1 Kill survivorship bias in the backtest universe  ★ highest-impact
- **What:** `us_data._fetch_sp500()` / `nse_data.get_index_constituents()` / crypto `get_universe()` all
  return **today's** members, then `run_full_backtest` applies them over 10y. Every strategy is thus
  backtested only on names that *survived and stayed in the index* — a systematic upward bias on
  win-rate / CAGR that the whole board inherits.
- **Why it improves efficiency:** this is the #1 reason live results will underperform the backtest.
  Fixing it de-inflates every advertised win-rate → the ranking, sizing (¼-Kelly reads win-prob), and
  the quality gate all become honest.
- **Where:** `pipeline_core/services/{us,nse}_data.py`, `pipeline_core/services/crypto_data.py`,
  `runner.run_full_backtest`.
- **How (pragmatic):** (a) short-term — apply a **survivorship haircut** to advertised win-rate/CAGR
  (a constant, documented, per-market drag) so the numbers stop over-promising; (b) proper — build a
  **point-in-time constituent set** (historical index membership + delisted tickers), even coarse
  (yearly snapshots). Even option (a) is a big honesty win for ~an afternoon.

### 1.2 Realistic, per-market, turnover-aware cost model
- **What:** `engine.simulate` charges a **flat 25 bps round-trip** for every market and horizon and
  models **no spread, no market impact, and — critically for the new crypto shorts — no perp funding
  / borrow**. High-turnover short-horizon strategies and crypto shorts are the most under-costed.
- **Why:** cost is the difference between a backtest edge and a live loss. The intraday bots already
  found themselves **net-negative after costs**; the daily board hasn't been stress-tested the same
  way. Under-costing systematically over-ranks churny strategies.
- **Where:** `engine.simulate` (`cost` term), `config.py` (add per-market cost bps + a crypto funding
  rate), `runner` (pass market cost through).
- **How:** cost = `spread_bps(market) + slippage(bps) + turnover×`; for crypto shorts add a daily
  **funding/borrow** drag over the holding period. Re-run the library; expect short-horizon and short
  ideas to drop in rank — which is the point.

### 1.3 Add the **north-star metric**: does rank predict return? (rank IC / decile spread)
- **What:** the tracker measures per-idea outcomes (`_realized_scoreboard`) and per-factor lift
  (`factor_attribution`), but **never** the one thing that defines board quality: *do higher-ranked
  ideas realize higher forward returns?* There is no rank **information coefficient** or
  **top-vs-bottom decile spread**.
- **Why:** without it you're flying blind on every other change — you can't tell if a tweak helped.
  This is the measurement that unblocks Tier 2.
- **Where:** new function in `idea_ranking.py` fed by the closed-trade ledger
  (`runner.tracked_ideas_status`), surfaced next to the existing Factor scorecard.
- **How:** on closed ideas, correlate entry-time `rank_score` (already stored) with
  `realized_return_pct` (Spearman IC) and report the mean return of the top-quintile vs bottom-quintile
  of rank. Cheap; immediately tells you whether the composite ranking is worth anything.

---

## Tier 2 — Make the learning layers actually fire (the accuracy lever)

### 2.1 Meta-labeling: a P(win | signal + context) model  ★ biggest accuracy lever
- **What:** every strategy emits a *primary* buy/short signal; there is no *secondary* model deciding
  **whether to take it**. The entry-time `factors` block is already logged on every tracked idea —
  that's a ready-made feature matrix for a López-de-Prado-style **meta-label** model.
- **Why:** meta-labeling is the single highest-ROI ML technique for exactly this setup (many
  rule-based signals + a rich context vector). It raises precision (kills false positives) without
  touching the strategies. `fusion.py` is 70% of the scaffolding already.
- **Where:** generalize `fusion.build_idea_model` from "combine conviction+winrate+sharpe" to a full
  meta-label classifier over the stored `factors` (regime, smart-money, dedup neighborhood, vol,
  horizon, side, age…); keep its existing **walk-forward "only active if it beats base-rate OOS"**
  gate.
- **How:** it already exists as a logistic model gated at 30 samples — the work is **feature
  enrichment** + letting `model_win_pct` drive both the sizer and a soft rank multiplier (it already
  can). The blocker is data volume → see 2.4.

### 2.2 Close the loop: factor attribution → rank weights
- **What:** `rank_w_earnings/news/sector/quality/smart_money` are **hand-set constants** (0.15–0.20).
  `factor_attribution()` already *measures* each factor's realized win-rate lift but **never feeds it
  back** ("measure-first" was deliberately deferred).
- **Why:** turns guessed weights into learned ones. A factor with negative measured lift should shrink
  toward 0 (or flip); one with strong lift should grow — directly improving rank IC (1.3).
- **Where:** `idea_ranking.rank_weights()` / `composite_multiplier()`, driven by
  `factor_attribution()` output with **shrinkage** (Bayesian pull to the current prior) and a hard cap
  so a noisy 10-sample estimate can't dominate.
- **How:** `w_i ← prior_i × (1 + k·shrunk_lift_i)`, refreshed when attribution has ≥ N samples per
  factor. Guardrail with the 1.3 metric: only adopt new weights if OOS rank-IC improves.

### 2.3 Put an out-of-sample gate on the **core** library (not just Discovery)
- **What:** the auto-**Discovery** layer has a proper OOS keep-gate + degradation guard, but the
  **39 curated strategies** vote on the live board using **full-sample** metrics. `robustness.py`
  (walk-forward + Monte-Carlo) exists but is an on-demand Strategy-Lab *display*, not a *gate*.
- **Why:** a strategy that's only full-sample-lucky still gets full conviction weight today. Gating
  votes by OOS consistency stops lucky strategies from inflating conviction.
- **Where:** compute `walk_forward` consistency per library strategy at run time; down-weight or drop
  votes from strategies whose OOS edge decays (mirror the crypto risk-gate pattern in
  `runner._strategy_risk_ok`).

### 2.4 Fix the cold-start (why 2.1–2.3 don't fire yet)
- **What:** calibration needs 20 closed trades, fusion 30, attribution 8 — the live ledger starts
  empty, so **all three are currently inactive** and the board runs on raw win-rates.
- **How:** seed the ledger from a **historical paper-replay** of the board (reconstruct the
  point-in-time board monthly over the backtest window — the `basket_backtest.py` anti-lookahead board
  reconstruction already proves this is feasible — and record the triple-barrier outcomes). Instantly
  gives calibration/fusion/attribution hundreds of labeled samples instead of waiting months of live
  trading.

---

## Tier 3 — Structural upside

### 3.1 Port the intraday "edge selects generation" loop to the daily board
- The intraday bots have a **triple-barrier edge harness** (`bots/backtest.py` → `edge.json`) that
  measures per-bot × timeframe × regime win-rate and then **gates/ranks** idea generation
  (`bots/selection.py`). The **daily** board has no equivalent — it trusts the strategy's own backtest.
  Building a daily analog (per-strategy × horizon × regime realized edge, feeding eligibility) is the
  same proven pattern applied one level up.

### 3.2 Portfolio-level risk, beyond correlation dedup
- Dedup removes correlated *names*; it doesn't **vol-target** the book or size by the covariance
  matrix. Add volatility targeting (scale exposure to a target book vol) and correlation-aware sizing
  so the ¼-Kelly book has stable risk across regimes.

### 3.3 Bring the new crypto **shorts** under a realized-edge gate
- The shorts are live but **unproven** and carry short-specific risk (funding, squeezes) that 1.2
  doesn't yet cost. Given the intraday "net-negative after costs" finding, treat crypto shorts as
  **watchlist-until-proven**: require a positive realized/triple-barrier edge (via 2.4's replay or a
  live sample) before they rank as *actionable*, not just *present*. Cheap guard, high downside
  protection.

---

## Suggested sequence (quick wins first)

1. **1.3 rank-IC metric** — a few hours, and it becomes the scoreboard for everything else.
2. **1.1 survivorship haircut** + **1.2 crypto/short cost + funding** — honesty on the numbers.
3. **2.4 historical paper-replay** to seed the ledger → **2.1/2.2/2.3 switch on**.
4. **3.3 gate crypto shorts** on realized edge (protects the feature just shipped).
5. **3.1 / 3.2** structural, once 1.3 shows where the edge actually is.

> Principle throughout: **measure before you tune.** 1.3 is the gate — adopt a change only when it
> moves out-of-sample rank-IC or the realized decile spread, never on backtest aesthetics alone.

---

## Next work queue (logged 2026-08-12 — NOT started)

Phase 1 is shipped. This is the ordered backlog for the next pass. Nothing here is executed yet.
Acceptance for every ranking change: it must move OOS **rank-IC / decile-spread** (1A), else it's reverted.

### Immediate — one open decision (metals haircut scoping)  ⏳ awaiting go-ahead
- The survivorship haircut (1D) is applied to **all** markets from one global knob, but **METALS is a
  fixed basket** (no index churn/delisting → no survivorship), so haircutting it is over-conservative.
  **Decision needed:** (rec) **exempt METALS / fixed-universe markets** — keep it for IN/US/CRYPTO; or
  make it **per-market** (`survivorship_haircut_pp_{in,us,metals,crypto}`).
- Files: `config.py` (a `market_haircut(market)` helper or per-market fields), `runner.horizon_ideas`
  (apply market-aware where `avg_wr` is computed ~L1208), `cost_disclosure` (report the market's value).
  Small (~1 file + a test). Ship with a unit test asserting METALS win-rate is unhaircut.

### Phase 2 — activate the learning layers (gated on rank-IC data accumulating; see Tier 2 above)
- [ ] **2A. Seed the ledger via historical board paper-replay** — reuse the anti-lookahead point-in-time
  reconstruction in `basket_backtest.py`; step monthly, open each idea, resolve with `_evaluate_idea_path`,
  write closed trades **with entry `factors` + `rank_score` + `entry_rank_pct`**. *Acceptance:* after the
  replay, `calibration.active` & `fusion.active` flip true and `rank_efficiency.active` is true (n ≥ 8).
  *Do this first in Phase 2 — it unblocks 2B/2C and the rank-IC readout.*
- [ ] **2B. Meta-labeling** — generalize `fusion.build_idea_model` to P(win | full stored factor block);
  keep its walk-forward OOS-beats-base gate; feed `model_win_pct` into the sizer (already prefers it) + a
  soft rank multiplier. *Acceptance:* rank-IC improves vs the 2A baseline.
- [ ] **2C. Attribution → rank weights** — drive `rank_w_*` from `factor_attribution` lift with shrinkage
  + a hard cap; adopt only if OOS rank-IC improves. Files: `idea_ranking.py`.
- [ ] **2D. OOS gate on the core library** — per-strategy `walk_forward` consistency at run time →
  down-weight/drop decaying strategies (mirror `runner._strategy_risk_ok`). Files: `runner`, `robustness.py`.

### Phase 3 — structural (queued; see Tier 3 above)
- [ ] **3A.** Daily "edge selects generation" loop (per-strategy × horizon × regime realized edge) — also
  the **auto-flip** for the 1C crypto-shorts gate once shorts show a positive realized edge.
- [ ] **3B.** Point-in-time universe (retires the 1D haircut at the source) — `us_data`/`nse_data`/`crypto_data`.
- [ ] **3C.** Portfolio vol-targeting + covariance-aware sizing (beyond correlation dedup).

*Suggested order:* metals decision → 2A → 2B/2C/2D (re-read rank-IC after each) → Phase 3.
