"""
Pydantic models for the portfolio management layer.

A `PortfolioSpec` is the *recipe* (basis + strategies + theme + risk + weighting).
`construct_portfolio` turns a spec into a `Portfolio` — a weighted, constraint-checked
basket of `PortfolioPosition`s plus a summary. The `Portfolio` is what gets persisted
(JSON payload) and later reconstructed for `rebalance_portfolio`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ──────────────────────────────────────────────────────────


class RiskAppetite(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class PortfolioBasis(str, Enum):
    FUNDAMENTAL = "fundamental"   # rank candidates from the fundamental screening strategies
    SYSTEMATIC = "systematic"     # rank candidates from the 35 trading strategies' live BUY signals
    BLEND = "blend"               # combine both — names in both get a conviction boost


class WeightingScheme(str, Enum):
    EQUAL = "equal"               # 1/N
    SCORE = "score"               # ∝ ranking score (composite / conviction / blended)
    CONVICTION = "conviction"     # ∝ number of strategies flagging the name


# ── Risk ───────────────────────────────────────────────────────────


class RiskProfile(BaseModel):
    """The concrete constraint bundle a `RiskAppetite` resolves to (after overrides)."""

    appetite: RiskAppetite
    cap_tier_index: str = Field(description="Universe index for this risk tier, e.g. 'NIFTY 200'")
    max_positions: int = Field(ge=1, le=100)
    per_name_cap: float = Field(gt=0, le=1, description="Max fraction of invested capital in one name")
    sector_cap: float = Field(gt=0, le=1, description="Max fraction of invested capital in one sector")
    cash_pct: float = Field(ge=0, lt=1, description="Reserved cash fraction (0-1)")
    default_stop_pct: float = Field(gt=0, lt=1, description="Fallback hard stop when a strategy defines none")
    allowed_categories: list[str] = Field(
        default_factory=list,
        description="Strategy categories permitted at this risk level (both libraries)",
    )


# ── Spec (the recipe) ──────────────────────────────────────────────


class PortfolioSpec(BaseModel):
    """Everything needed to (re)construct a portfolio."""

    name: str
    market: str = Field(default="IN", description="Asset class: IN | US | METALS | CRYPTO")
    basis: PortfolioBasis = PortfolioBasis.BLEND
    strategy_ids: list[str] = Field(
        default_factory=list,
        description="Fundamental screen ids and/or trading strategy ids, per basis",
    )
    theme: Optional[str] = Field(None, description="Theme slug, e.g. 'green-energy' (None = no tilt)")
    risk: RiskAppetite = RiskAppetite.BALANCED
    risk_overrides: Optional[dict] = Field(
        None, description="Partial RiskProfile fields to override the preset (advanced)"
    )
    weighting: WeightingScheme = WeightingScheme.SCORE
    max_positions: Optional[int] = Field(None, description="Override the risk preset's max_positions")
    notes: Optional[str] = None


# ── Constructed portfolio ──────────────────────────────────────────


class PortfolioPosition(BaseModel):
    ticker: str
    name: Optional[str] = None
    sector: Optional[str] = None
    weight: float = Field(description="Fraction of the whole portfolio (invested part; cash is separate)")
    conviction: int = Field(default=1, description="How many strategies flagged this name")
    source_strategies: list[str] = Field(default_factory=list)
    entry_price: Optional[float] = None
    stop_pct: Optional[float] = None
    stop_price: Optional[float] = None
    stop_source: Optional[str] = None      # 'strategy' | 'risk-default'
    target_pct: Optional[float] = None
    score: Optional[float] = None          # ranking score used for sizing
    rationale: Optional[str] = None


class Portfolio(BaseModel):
    id: Optional[str] = None
    name: str
    spec: PortfolioSpec
    positions: list[PortfolioPosition] = Field(default_factory=list)
    cash_pct: float = 0.0
    summary: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    disclaimer: Optional[str] = None


# ── Rebalance ──────────────────────────────────────────────────────


class RebalanceAction(BaseModel):
    ticker: str
    action: str                            # BUY | ADD | TRIM | HOLD | EXIT
    held_weight: float = 0.0
    target_weight: float = 0.0
    drift_pct: float = 0.0                  # (target - held) in percentage points
    current_price: Optional[float] = None
    entry_price: Optional[float] = None
    return_pct: Optional[float] = None
    reason: Optional[str] = None


class RebalanceResult(BaseModel):
    portfolio_id: Optional[str] = None
    name: str
    actions: list[RebalanceAction] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    as_of: Optional[str] = None
    disclaimer: Optional[str] = None
