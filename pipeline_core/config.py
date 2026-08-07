"""
Application configuration — settings, API keys, feature flags.
All secrets loaded from environment variables or .env file.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

# Project root = parent of the pipeline_core package. Override data location
# with STOCKSENSE_DATA_DIR when embedding the core elsewhere (e.g. the agy app).
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STOCKSENSE_DATA_DIR", BASE_DIR / "data"))
CACHE_DIR = DATA_DIR / "cache"
REPORTS_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "stocksense.db"

for _d in [DATA_DIR, CACHE_DIR, REPORTS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """App settings — loaded from .env or environment variables."""

    # App
    app_name: str = "StockSense"
    debug: bool = True

    # LLM Provider: "openai", "anthropic", "gemini", "ollama", or "none" (rules-only mode)
    llm_provider: str = Field(default="none")
    openai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    ollama_base_url: str = Field(default="http://localhost:11434")
    # Gemini speaks the OpenAI-compatible API — reused via the openai SDK (no extra deps).
    gemini_base_url: str = Field(default="https://generativelanguage.googleapis.com/v1beta/openai/")
    llm_model: str = Field(default="gpt-4o-mini")
    # Circuit breaker: after a quota/429 (or this many consecutive failures) skip the provider for
    # the cooldown so per-item fan-outs fall back to rules-only fast instead of retrying a dead API.
    llm_breaker_cooldown_s: int = 120
    llm_breaker_threshold: int = 3
    # Provider-SDK retries per call — 0 so a 429 fails fast into the breaker/lexicon rather than
    # burning the SDK's internal exponential backoff on every doomed call.
    llm_max_retries: int = 0

    # SEC EDGAR — free historical institutional holdings (13F filings) for US equities.
    # SEC's fair-access policy REQUIRES a descriptive User-Agent with real contact info on
    # every request; set your own so you aren't throttled/blocked. OpenFIGI maps CUSIP↔ticker
    # (the 13F key), free without a key at low volume — a key just raises the rate limit.
    sec_user_agent: str = Field(default="StockSense/1.0 (contact: set sec_user_agent in .env)")
    openfigi_api_key: str = Field(default="")

    # Smart-money / coattail layer — generalizes the 13F ownership signal into a per-market
    # "follow the smart money" overlay (see pipeline_core/services/smart_money.py): who the proven,
    # informed money is and whether they're accumulating, used as an extra INDEPENDENT confluence
    # family (a pillar), a live badge, and a backtestable follow-the-smart-money strategy. Each
    # source is keyless; fragile/paid ones default OFF and land behind their flag later.
    smart_money_enabled: bool = True
    sm_us_gurus_enabled: bool = True        # fold famed gurus into the US 13F basket (edgar_13f.GURU_MANAGERS)
    sm_metals_cot_enabled: bool = True      # CFTC Commitments of Traders — commercials positioning (Phase 1)
    sm_crypto_flows_enabled: bool = True    # exchange netflows + Hyperliquid top-trader positioning (Phase 2)
    sm_in_bulk_deals_enabled: bool = True   # NSE/BSE bulk & block deals + FII/DII flows (Phase 3)
    sm_in_ace_enabled: bool = False         # fragile: ace-investor shareholding-pattern scrape (Phase 4)
    sm_crypto_labels_enabled: bool = False  # best-effort/paid: on-chain smart-money wallet labels (Phase 4)
    # CFTC COT (metals): the commercial net position is normalized to a 0–100 COT index over this
    # lookback; ≥ high ⇒ commercials heavily net-long (bullish coattail), ≤ low ⇒ net-short (bearish).
    cot_lookback_weeks: int = 156           # ~3 years of weekly reports for the range normalization
    cot_index_high: float = 75.0
    cot_index_low: float = 25.0
    # Crypto (Hyperliquid): aggregate the live positions of the top winning traders into a per-coin
    # net-directional bias — the literal "copy better traders" signal (live only, no backtest series).
    hl_top_traders: int = 40                # how many top-PnL accounts to read positions from
    hl_window: str = "month"                # leaderboard window to rank winners by (day|week|month|allTime)
    hl_min_account_usd: float = 1_000_000.0  # ignore small accounts — whale conviction, not noise

    # Cache TTL (seconds)
    cache_ttl_price: int = 900            # 15 minutes (history bars — effectively immutable intraday)
    cache_ttl_quote: int = 90            # live mark-to-market price — kept fresh for realistic P&L
    cache_ttl_fundamentals: int = 86400   # 1 day
    cache_ttl_news: int = 3600            # 1 hour
    cache_ttl_holdings: int = 86400       # 1 day
    cache_ttl_fear_greed: int = 3600      # 1 hour (crypto Fear & Greed index)
    cache_ttl_regime: int = 3600          # 1 hour (per-market risk-on/off regime gauge)

    # Equity data resilience — yfinance has no SLA and rate-limits (HTTP 429) under load, which used
    # to empty a whole backtest/board. The equity price fetch now runs through the provider fallback
    # chain (retries + backoff + health at /health/providers) with a keyless Stooq fallback, and
    # serves the last-good frame (flagged stale) rather than nothing when every live source fails.
    price_fetch_retries: int = 2          # transient-failure retries per provider (exp backoff)
    equity_fallback_enabled: bool = True  # try Stooq daily EOD when yfinance yields nothing
    cache_ttl_laststood: int = 604800     # 7 days — how long a last-good frame is kept for stale-serve

    # Screening defaults
    default_market: str = "IN"            # "IN" | "US" | "METALS" | "CRYPTO"
    default_index: str = "NIFTY 50"
    max_candidates: int = 20
    analysis_depth: str = "standard"      # "quick" | "standard" | "deep"

    # Concurrency — bound parallel outbound market-data calls
    ingestion_concurrency: int = 8
    analysis_concurrency: int = 6

    # Background idea generation — a fast signals-only re-scan (no metrics recompute) keeps the
    # Trade Ideas board fresh so new setups aren't missed. Runs while the server is up.
    auto_refresh_enabled: bool = True
    auto_refresh_interval_min: int = 10        # how often the background loop re-scans signals
    auto_refresh_markets: str = "IN,US,CRYPTO"  # comma-separated markets to keep fresh

    # Composite score weights (must sum to 1.0; risk component is inverted)
    weight_fundamental: float = 0.40
    weight_technical: float = 0.30
    weight_sentiment: float = 0.15
    weight_risk: float = 0.15

    # Position sizing — capped fractional Kelly turns each idea's measured edge (win prob +
    # reward:risk) into a per-trade risk % and a portfolio allocation %. See pipeline_core/sizing.py.
    sizing_enabled: bool = True
    sizing_kelly_fraction: float = 0.25       # ¼-Kelly (conservative, industry-standard default)
    sizing_max_position_pct: float = 0.20     # ≤20% of capital in any one idea
    sizing_risk_per_trade_pct: float = 0.015  # ≤1.5% of equity risked per idea
    sizing_max_book_risk_pct: float = 0.06    # ≤6% total risk across a single idea book

    # Correlation-aware de-duplication — collapse correlated ideas (e.g. alts that all follow BTC)
    # to the single best-ranked one per cluster before sizing. See pipeline_core/dedup.py.
    dedup_enabled: bool = True                # intraday bot books
    dedup_daily_enabled: bool = True          # daily Trade Ideas board (independent toggle)
    dedup_corr_threshold: float = 0.8         # trailing-return correlation ≥ this ⇒ same cluster

    # Intraday idea curation — turn the (long + short) bot books into ONE small, ranked "best-of"
    # shortlist, fusing a sample-size-aware (Wilson) win rate, measured edge, agreement, and live
    # sentiment (per-coin news + market-wide Fear & Greed). Quality over quantity: a user can't act
    # on 40 setups. See pipeline_core/bots/selection.py + pipeline_core/bots/ideas.py.
    ideas_top_k: int = 5                      # size of the curated shortlist (long + short mixed)
    ideas_min_score: float = 0.0              # drop shortlisted ideas below this composite score
    ideas_candidate_pool: int = 8             # per-side coins we fetch sentiment for (bounds I/O)
    ideas_sentiment_enabled: bool = True      # blend live news + Fear & Greed into the ranking

    # Market-regime gate — a per-market risk-on/off gauge (benchmark 200-DMA + a vol/macro factor)
    # that trades the daily ideas WITH the market tide: regime-aligned longs rank first, and in
    # risk-off each long gets a soft ¼-Kelly size haircut (never hard-hidden). A neutral reading
    # (offline / gate off / tilt≈0) reproduces the ungated board exactly. See services/market_regime.py.
    regime_gate_enabled: bool = True          # blend the market regime into daily idea ranking + sizing
    regime_rank_weight: float = 0.25          # how strongly tilt shifts conviction in the ranking
    regime_size_weight: float = 0.5           # risk-off size haircut strength (applied only when tilt<0)
    regime_size_floor: float = 0.4            # never trim a position below this fraction of its base size

    # Crypto daily-idea risk gate — crypto strategies backtest with catastrophic drawdowns (−74%…−100%),
    # so a strategy's BUY signal only counts toward an idea's conviction when its backtested risk clears
    # these floors. Applied only for the CRYPTO market; equities (modest DDs) are unaffected. Never
    # empties a bucket — if all would be gated, the least-bad by drawdown are kept.
    crypto_idea_dd_floor: float = -95.0       # drop votes from strategies with max_drawdown_pct ≤ this
    crypto_idea_sharpe_floor: float = 0.2     # …and from strategies below this backtested Sharpe

    # Daily-idea composite ranking — soft accuracy factors layered on conviction × regime, each a
    # bounded ± multiplier (see pipeline_core/backtest/idea_ranking.py). 0 disables a factor; all-zero
    # reproduces the pre-factor board. Applied live (a re-rank pool is enriched, re-scored, then capped).
    rank_pool_mult: int = 2                   # keep this × max_per_bucket as the re-rank pool before capping
    # Quality gate — the antidote to "mixing everything": after the composite re-rank, DROP ideas the
    # soft factors collectively argue against (multiplier below this floor = fighting ≥2 headwinds), so
    # the board shows fewer, higher-conviction trades instead of a diluted top-10. 1.0 = only supportive
    # ideas; 0 disables the gate (show the full capped list). ~0.80 cuts multi-headwind conflicts only.
    rank_min_multiplier: float = 0.80
    # Earnings: a mild risk-awareness de-rank + a ⚠️ flag. NOT a proven-alpha lever — a 10y backtest
    # showed a blanket pre-earnings entry blackout removes trades without improving win-rate/drawdown,
    # so this is a small risk-preference nudge (the flag is the main value), tune to taste / 0 to disable.
    rank_w_earnings: float = 0.15
    # News sentiment: bullish headlines lift an idea, bearish trim it. Live-only (no news history →
    # not backtestable); adds a per-name news fetch to the board load (cached ~1h). 0 disables it
    # (and skips the fetch). A soft re-rank, not a veto.
    rank_w_news: float = 0.20
    # Sector rotation: lift ideas whose sector is leading the benchmark, trim laggards (backtestable
    # via sector-ETF history). US-only today (SPDR sector proxies); other markets have no sector map
    # → neutral. 0 disables it (and skips the sector fetch).
    rank_w_sector: float = 0.20
    # Fundamental quality: lift high-ROE / low-leverage / healthy-margin names, trim weak-quality
    # technical breakouts (from get_stock_info; equity-only, ~4y statement history). 0 disables it.
    rank_w_quality: float = 0.15
    # Factor attribution — measure each soft factor's REALIZED lift from the closed-idea ledger
    # (win-rate when the factor was supportive vs adverse at entry). A factor's supportive/adverse
    # cohort must each reach this many closed trades before a lift number is reported (else "need
    # more data"). Kept modest since the closed set splits several ways; raise for stricter evidence.
    attribution_min_samples: int = 8

    # Multi-pillar confluence — reframe "quality" from DEPTH in one family (conviction = how many
    # technical strategies agree) to BREADTH across independent families (technical + fundamental
    # quality + institutional accumulation + regime tailwind + news + sector). An idea clearing
    # ≥ pillar_min_actionable of the families available this market is ACTIONABLE (a small, high-
    # conviction set); the rest drop to a per-column Watchlist. The bar is clamped to availability so
    # thin-pillar markets (crypto/metals) still surface actionable ideas. See idea_ranking.idea_pillars.
    pillar_tiering_enabled: bool = True        # split the board into Actionable / Watchlist tiers
    pillar_min_actionable: int = 3             # families an idea must confirm (technical + ≥2 more)

    # "Normal drawdown" band — surface how far a WINNING backtest trade of the backing strategies
    # typically dipped before it worked (median |max-adverse-excursion|), as hold-through guidance on
    # the idea card + a within/beyond flag while held. Anti-panic info, NOT a stop. The engine always
    # computes it (negligible cost); this only gates surfacing it. See engine.compute_metrics.
    drawdown_band_enabled: bool = True

    # Curated basket — the daily analog of the intraday shortlist (bots/selection.py): distil the
    # full 30-idea board (10 × short/medium/long) into ONE small, deployable best-of of 3–5 trades
    # sized as a single cross-horizon ¼-Kelly book (allocations sum to ≤100%). Balanced/risk-adjusted:
    # each idea scores on expected value × sample/agreement confidence × its existing rank_score, then
    # a GREEDY DIVERSIFIED pick skips names in the same correlation cluster (cross-horizon, reusing the
    # board's dedup clusters) with soft sector/horizon spread. Regime scales the size (more names in
    # risk-on, fewer in risk-off) and it's STICKY across days (hysteresis) so it's actually holdable.
    # See pipeline_core/backtest/basket.py. basket_enabled=False leaves the board untouched.
    basket_enabled: bool = True
    basket_size_min: int = 3                   # basket size in risk-off (fewer, higher-conviction bets)
    basket_size_max: int = 5                   # basket size in risk-on (neutral uses the midpoint)
    basket_w_ev: float = 0.45                  # weight on expected value (p·target − (1−p)·stop)
    basket_w_confidence: float = 0.30          # weight on sample/agreement confidence (Wilson/agreement)
    basket_w_quality: float = 0.25             # weight on the idea's per-horizon-normalized rank_score
    basket_sector_cap: int = 2                 # soft cap: at most this many names per sector (equity)
    basket_hysteresis_margin: float = 0.10     # a challenger must beat an incumbent's score by >this to swap it in
    # Deploy the basket as a NAV-tracked Portfolio (reuses the portfolio layer). The live deployed book
    # re-syncs on a weekly cadence (idempotent, cost-basis-preserving) so it tracks the sticky basket
    # without churning; NAV still snapshots daily. See runner.deploy_basket / bots/crypto_momentum.deploy.
    basket_autodeploy_enabled: bool = True     # auto re-sync of an already-deployed basket portfolio
    basket_redeploy_days: int = 30             # re-deploy an existing basket portfolio at most this often
    # Backtest of the basket CONSTRUCTION method — a held-basket, periodically-rebalanced historical sim
    # with anti-lookahead (point-in-time signals/regime/clusters). Validates conviction aggregation +
    # cross-horizon diversification + regime-scaled ¼-Kelly + hysteresis (NOT the full-sample calibration
    # layer, which would leak). See pipeline_core/backtest/basket_backtest.py.
    # CADENCE: the real US backtest (2019→2026) showed rebalance frequency is the dominant lever —
    # WEEKLY(5d) −5% CAGR / −20% alpha (turnover eats it), MONTHLY(21d) +17% CAGR / +1.9% alpha /
    # Sharpe 1.06, QUARTERLY(63d) +11% / −4% alpha. Monthly is the sweet spot (default), since turnover
    # cost is ~linear in rebalance count (weekly does ~4× the trades of monthly for similar per-rebalance
    # turnover). Deploy re-sync (above) matches at ~monthly.
    basket_backtest_years: int = 8             # history span for the basket backtest
    basket_rebalance_days: int = 21            # rebalance cadence in trading days (~monthly — see CADENCE)
    basket_backtest_cost_bps: float = 25.0     # round-trip turnover cost per unit weight traded (bps)

    # My Hold Basket — the COMMITTED, conviction-held counterpart to the (live-view) daily basket. Built
    # for STAYING POWER (higher conviction floor + longer-horizon tilt + much wider hysteresis), it's
    # deployed as a frozen NAV-tracked portfolio that only changes on a MATERIAL event (a holding hit its
    # stop/target, its thesis broke, or a dramatically-better idea clears the displacement bar) — shown as
    # a reasoned diff you approve, and alerted once. See build_basket(hold_mode=True) + hold_rebalance_plan.
    hold_basket_enabled: bool = True
    hold_min_conviction: int = 2               # only names ≥ this many agreeing strategies are hold-worthy
    hold_hysteresis_margin: float = 0.30       # incumbent stickiness (vs the daily 0.10) — hold, don't churn
    hold_displacement_conviction_gap: int = 2  # a fresh name only bumps a holding if its conviction is ≥ this higher
    hold_size_min: int = 3                     # hold-book size in risk-off
    hold_size_max: int = 5                     # hold-book size in risk-on (neutral = midpoint)
    # (the per-horizon score tilt favouring longer holds lives in basket.HOLD_HORIZON_FACTOR)

    # Multi-bagger screen — a fundamentals-first, long-horizon idea generator (a sub-tab of Trade
    # Ideas; see pipeline_core/multibagger.py). Scores a broad mid/small-cap universe on six pillars
    # and surfaces high-growth, high-quality, smaller-cap compounders. Equity markets only.
    multibagger_enabled: bool = True
    multibagger_max_universe: int = 500       # cap names fetched in the (cheap) info stage
    multibagger_top_n: int = 20               # candidates shown
    multibagger_deep_top_n: int = 25          # survivors that pay for the deep (statements) stage
    multibagger_min_coverage: float = 0.5     # drop a name scored on < this fraction of the 6 pillars
    # Pillar weights (composite = coverage-renormalised weighted mean). Size is the defining
    # multi-bagger tilt (smaller = more runway); technical is a small, bounded timing overlay only.
    multibagger_w_growth: float = 0.30
    multibagger_w_quality: float = 0.22
    multibagger_w_balance: float = 0.13
    multibagger_w_value: float = 0.10
    multibagger_w_size: float = 0.15
    multibagger_w_ownership: float = 0.10
    multibagger_w_technical: float = 0.08     # max ±8% nudge — can't reorder the fundamental leaders

    # Hold-coaching alerts — the ACTIVE half of "take & stick with it", on the background alert loop
    # (pipeline_core/alerts.py evaluate_market). Anti-panic + take-the-trade nudges, in-app only.
    alert_hold_coach_enabled: bool = True     # alert when an open position is BEYOND its normal-dip band (review thesis)
    alert_triggered_enabled: bool = True      # alert when a watchlist idea ENTERS its buy zone (triggered — consider entering)

    # Reports
    report_top_n: int = 1                 # how many top candidates get reports

    # Authentication & user accounts. Per-user data (portfolios, watchlist, paper,
    # screens, alerts) is scoped by the logged-in account; the MCP server and the
    # background refresh loop act as the "owner". See pipeline_core/services/auth.py.
    auth_session_ttl_days: int = 30           # session lifetime (sliding window)
    auth_allow_signup: bool = True            # allow new (non-owner) registrations
    auth_cookie_secure: bool = False          # set True when served over HTTPS (hosting)
    owner_email: str = Field(default="")      # optional: bootstrap the owner account from env
    owner_password: str = Field(default="")   # (paired with owner_email)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    def validate_weights(self) -> None:
        total = (
            self.weight_fundamental
            + self.weight_technical
            + self.weight_sentiment
            + self.weight_risk
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Composite score weights must sum to 1.0, got {total}")


settings = Settings()
