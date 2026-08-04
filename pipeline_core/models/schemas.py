"""
Pydantic schemas for pipeline request/response models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, PrivateAttr


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ──────────────────────────────────────────────────────────


class AnalysisDepth(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SignalType(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class RiskLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class SEBICategory(str, Enum):
    LARGE_CAP = "Large Cap"
    MID_CAP = "Mid Cap"
    SMALL_CAP = "Small Cap"


# ── Filter Schemas ─────────────────────────────────────────────────


class ScreenFilters(BaseModel):
    """Structured filters for Indian stock screening."""

    # Universe
    index: Optional[str] = Field(None, description="Index: NIFTY 50, NIFTY 200, NIFTY 500, etc.")
    sebi_category: Optional[SEBICategory] = None
    sector: Optional[str] = None
    industry: Optional[str] = None

    # Valuation
    pe_ratio_min: Optional[float] = None
    pe_ratio_max: Optional[float] = None
    pb_ratio_max: Optional[float] = None
    ev_ebitda_max: Optional[float] = None
    peg_ratio_max: Optional[float] = None
    dividend_yield_min: Optional[float] = None

    # Growth
    revenue_growth_min: Optional[float] = Field(None, description="YoY revenue growth (decimal, e.g. 0.15 = 15%)")
    profit_growth_min: Optional[float] = None
    eps_growth_3y_min: Optional[float] = None
    sales_growth_3y_min: Optional[float] = None

    # Profitability (decimals unless noted)
    roe_min: Optional[float] = None
    roce_min: Optional[float] = None
    opm_min: Optional[float] = None
    npm_min: Optional[float] = None

    # India-specific
    promoter_holding_min: Optional[float] = Field(None, description="Min promoter holding (decimal, e.g. 0.5 = 50%)")
    promoter_pledge_max: Optional[float] = Field(None, description="Max promoter pledge (decimal, e.g. 0.05 = 5%)")
    fii_holding_change_positive: Optional[bool] = None
    dii_holding_change_positive: Optional[bool] = None
    delivery_pct_min: Optional[float] = None

    # Financial health
    debt_to_equity_max: Optional[float] = None
    interest_coverage_min: Optional[float] = None
    piotroski_score_min: Optional[int] = Field(None, ge=0, le=9)
    fcf_yield_min: Optional[float] = None

    # Technical
    rsi_min: Optional[float] = None
    rsi_max: Optional[float] = None
    price_above_200dma: Optional[bool] = None
    price_above_50dma: Optional[bool] = None
    macd_bullish: Optional[bool] = None
    near_52w_high_pct: Optional[float] = Field(None, description="Within X% of 52-week high")
    near_52w_low_pct: Optional[float] = Field(None, description="Within X% of 52-week low")
    volume_spike_min: Optional[float] = Field(None, description="Volume vs 20-day avg multiple")

    # Market cap (INR Crore)
    market_cap_min_cr: Optional[float] = None
    market_cap_max_cr: Optional[float] = None

    def active_filter_names(self) -> list[str]:
        """Names of fields explicitly set (excluding universe selectors)."""
        skip = {"index", "sebi_category"}
        return [
            name
            for name, value in self.model_dump(exclude_unset=True).items()
            if name not in skip and value is not None
        ]


# ── Pipeline Schemas ───────────────────────────────────────────────


class PipelineRequest(BaseModel):
    """Request to start a new analysis pipeline."""

    market: str = Field(default="IN", description="Asset class: IN | US | METALS | CRYPTO")
    query: Optional[str] = Field(None, description="Natural language query")
    filters: ScreenFilters = Field(default_factory=ScreenFilters)
    analysis_depth: AnalysisDepth = AnalysisDepth.STANDARD
    max_candidates: int = Field(default=10, ge=1, le=50)
    include_report: bool = True


class FilterExplanation(BaseModel):
    """Per-filter pass/fail detail for explainability."""

    filter_name: str
    passed: bool
    actual_value: Optional[float | str | bool] = None
    # True when the data source was unavailable — the filter is excluded
    # from the match count rather than counted against the stock.
    indeterminate: bool = False


class StockScore(BaseModel):
    """Composite score for a screened stock."""

    ticker: str
    name: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    market_cap_cr: Optional[float] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None

    # Filter match
    filters_matched: int = 0
    filters_total: int = 0
    filter_match_pct: float = 0.0
    filter_explanations: list[FilterExplanation] = Field(default_factory=list)

    # Scores (0-100)
    fundamental_score: Optional[float] = None   # None for price-only markets (metals/crypto)
    technical_score: Optional[float] = None
    sentiment_score: Optional[float] = None
    risk_score: Optional[float] = None
    momentum_score: Optional[float] = None      # price-only markets: trend/momentum sub-score
    composite_score: Optional[float] = None

    # Signals
    technical_signal: Optional[SignalType] = None
    risk_level: Optional[RiskLevel] = None

    # Key metrics
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    roe: Optional[float] = None
    roce: Optional[float] = None
    debt_to_equity: Optional[float] = None
    promoter_holding: Optional[float] = None
    promoter_pledge: Optional[float] = None
    dividend_yield: Optional[float] = None
    rsi: Optional[float] = None

    # Evidence bundle from the analysis agents (never serialized) —
    # carried from the analysis stage to the report stage.
    _analysis: dict = PrivateAttr(default_factory=dict)


class PipelineRun(BaseModel):
    """Pipeline run state."""

    id: str
    query: Optional[str] = None
    filters: ScreenFilters = Field(default_factory=ScreenFilters)
    status: PipelineStatus = PipelineStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: Optional[datetime] = None
    current_stage: Optional[str] = None
    progress_pct: float = 0.0
    candidates_found: int = 0
    results: list[StockScore] = Field(default_factory=list)
    report_files: list[str] = Field(default_factory=list)
    llm_mode: str = "rules-only"
    error: Optional[str] = None


class PipelineProgress(BaseModel):
    """WebSocket progress message."""

    pipeline_id: str
    stage: str
    progress_pct: float
    message: str
    candidates_found: Optional[int] = None


# ── Stock Detail Schemas ──────────────────────────────────────────


class StockDetail(BaseModel):
    """Detailed stock info for individual stock view."""

    ticker: str
    name: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None
    market_cap_cr: Optional[float] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    week_52_high: Optional[float] = None
    week_52_low: Optional[float] = None
    volume: Optional[int] = None
    avg_volume: Optional[int] = None

    # Fundamentals
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    ev_ebitda: Optional[float] = None
    roe: Optional[float] = None
    roce: Optional[float] = None
    opm: Optional[float] = None
    npm: Optional[float] = None
    debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None
    dividend_yield: Optional[float] = None
    eps: Optional[float] = None
    book_value: Optional[float] = None
    free_cash_flow: Optional[float] = None
    piotroski_score: Optional[int] = None

    # India-specific
    promoter_holding: Optional[float] = None
    promoter_pledge: Optional[float] = None
    fii_holding: Optional[float] = None
    dii_holding: Optional[float] = None
    delivery_pct: Optional[float] = None


class SavedScreen(BaseModel):
    """A saved filter preset."""

    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    filters: ScreenFilters
    created_at: datetime = Field(default_factory=utcnow)
