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

    # SEC EDGAR — free historical institutional holdings (13F filings) for US equities.
    # SEC's fair-access policy REQUIRES a descriptive User-Agent with real contact info on
    # every request; set your own so you aren't throttled/blocked. OpenFIGI maps CUSIP↔ticker
    # (the 13F key), free without a key at low volume — a key just raises the rate limit.
    sec_user_agent: str = Field(default="StockSense/1.0 (contact: set sec_user_agent in .env)")
    openfigi_api_key: str = Field(default="")

    # Cache TTL (seconds)
    cache_ttl_price: int = 900            # 15 minutes (history bars — effectively immutable intraday)
    cache_ttl_quote: int = 90            # live mark-to-market price — kept fresh for realistic P&L
    cache_ttl_fundamentals: int = 86400   # 1 day
    cache_ttl_news: int = 3600            # 1 hour
    cache_ttl_holdings: int = 86400       # 1 day
    cache_ttl_fear_greed: int = 3600      # 1 hour (crypto Fear & Greed index)
    cache_ttl_regime: int = 3600          # 1 hour (per-market risk-on/off regime gauge)

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
