"""
Risk-appetite presets and resolution.

Three presets (conservative / balanced / aggressive) map a `RiskAppetite` to a
concrete `RiskProfile` — the universe tier, position count, per-name & sector
weight caps, cash buffer, fallback stop, and which strategy *categories* are
permitted. Advanced callers may override any field via `risk_overrides`.

`allowed_categories` gates BOTH strategy libraries' `category` fields:
  fundamental (pipeline_core/strategies.py): Quality, Value, Growth, Momentum,
    Contrarian, India-specific, Income, Defensive
  trading (pipeline_core/backtest/strategies.py): Mean Reversion, Trend,
    Momentum, Breakout, Volume, Contrarian
"""

from __future__ import annotations

from typing import Optional

from pipeline_core.portfolio.models import RiskAppetite, RiskProfile

# Universe index per risk tier. NIFTY 100 keeps conservative in large-caps;
# aggressive opens up the full NIFTY 500. (get_index_tickers falls back to
# NIFTY 50 loudly if a tier index can't be resolved live.)
CAP_TIER_INDEX: dict[RiskAppetite, str] = {
    RiskAppetite.CONSERVATIVE: "NIFTY 100",
    RiskAppetite.BALANCED: "NIFTY 200",
    RiskAppetite.AGGRESSIVE: "NIFTY 500",
}

# Categories permitted at each tier (cumulative: balanced ⊃ conservative, etc.).
_CONSERVATIVE_CATS = ["Quality", "Value", "Income", "Defensive", "India-specific", "Trend"]
_BALANCED_CATS = _CONSERVATIVE_CATS + ["Growth", "Momentum", "Breakout"]
_AGGRESSIVE_CATS = _BALANCED_CATS + ["Contrarian", "Mean Reversion", "Volume"]


RISK_PRESETS: dict[RiskAppetite, RiskProfile] = {
    RiskAppetite.CONSERVATIVE: RiskProfile(
        appetite=RiskAppetite.CONSERVATIVE,
        cap_tier_index=CAP_TIER_INDEX[RiskAppetite.CONSERVATIVE],
        max_positions=12,
        per_name_cap=0.12,
        sector_cap=0.30,
        cash_pct=0.15,
        default_stop_pct=0.08,
        allowed_categories=_CONSERVATIVE_CATS,
    ),
    RiskAppetite.BALANCED: RiskProfile(
        appetite=RiskAppetite.BALANCED,
        cap_tier_index=CAP_TIER_INDEX[RiskAppetite.BALANCED],
        max_positions=15,
        per_name_cap=0.10,
        sector_cap=0.35,
        cash_pct=0.08,
        default_stop_pct=0.10,
        allowed_categories=_BALANCED_CATS,
    ),
    RiskAppetite.AGGRESSIVE: RiskProfile(
        appetite=RiskAppetite.AGGRESSIVE,
        cap_tier_index=CAP_TIER_INDEX[RiskAppetite.AGGRESSIVE],
        max_positions=20,
        per_name_cap=0.08,
        sector_cap=0.40,
        cash_pct=0.02,
        default_stop_pct=0.13,
        allowed_categories=_AGGRESSIVE_CATS,
    ),
}

# Fields a caller is allowed to override (appetite itself is not overridable).
_OVERRIDABLE = {
    "cap_tier_index", "max_positions", "per_name_cap", "sector_cap",
    "cash_pct", "default_stop_pct", "allowed_categories",
}


def resolve_risk(appetite: RiskAppetite, overrides: Optional[dict] = None) -> RiskProfile:
    """The concrete profile for an appetite, with any override fields merged on top."""
    base = RISK_PRESETS[appetite].model_copy(deep=True)
    if not overrides:
        return base
    data = base.model_dump()
    for key, value in overrides.items():
        if key in _OVERRIDABLE and value is not None:
            data[key] = value
    return RiskProfile.model_validate(data)


def list_risk_profiles() -> list[dict]:
    """Preset catalog for the config surface (REST/MCP/UI)."""
    return [RISK_PRESETS[a].model_dump() for a in RiskAppetite]
