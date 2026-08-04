"""
Daily-idea **composite ranking** — soft factors layered on the base conviction score.

The daily Trade Ideas board ranks each idea by conviction × the market-regime tilt. This module
adds the extra accuracy levers as **soft multipliers** on that base — mirroring the intraday
shortlist (`bots/selection.py`): a factor that argues against an idea scales its score *down* (so
it slips down the board or off it) but never hard-vetoes a high-conviction technical, and a
supportive factor lifts it.

    rank_score = base_score · Π (1 + wᵢ · factorᵢ)      each factorᵢ ∈ [−1, +1]

Every factor degrades to `None` (→ contributes ×1) when its data is missing or its weight is 0, so
**all-neutral reproduces the pre-factor board exactly** (regression-safe). Pure functions of numbers
the idea already carries → network-free and trivially unit-testable. Weights live in `settings`
(`rank_w_*`) and are disclosed via `rank_weights()`.

Factors (long-only daily board):
  • earnings — event risk: a soft penalty that ramps up as an earnings report approaches.
  • news     — per-name news sentiment (bullish lifts, bearish trims). Live-only.
  • sector   — the idea's sector relative strength vs the benchmark (rotation).
  • quality  — a fundamental-quality read (weak-quality technical breakouts get trimmed).
"""

from __future__ import annotations

import math
from typing import Optional

from pipeline_core.config import settings

# Sub-term shape constants (weights themselves are in settings.rank_w_*).
EARNINGS_WINDOW_DAYS = 7      # penalty ramps over the N calendar days before earnings
SECTOR_RS_SCALE = 10.0       # sector out/under-performance (pp, 3-month) that maps to a strong tilt

FACTOR_NAMES = ("earnings", "news", "sector", "quality")


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── Individual factors (each returns a value in [−1, +1], or None = neutral) ──

def earnings_factor(days_to_earnings: Optional[int], window: int = EARNINGS_WINDOW_DAYS) -> Optional[float]:
    """Event-risk penalty as earnings approach: 0 at `window` days out, ramping to −1 the day of.
    None (no effect) when earnings are further out than the window or unknown — a technical setup
    entered right before a binary earnings print is a coin-flip we want to de-prioritise."""
    if days_to_earnings is None or days_to_earnings < 0 or days_to_earnings > window:
        return None
    return round(-(1.0 - days_to_earnings / float(window)), 4)


def news_factor(news_score: Optional[float]) -> Optional[float]:
    """News sentiment as a symmetric tilt (long-only board): bullish (>50) lifts, bearish trims.
    `news_score` is 0–100 with 50 neutral. None → neutral."""
    if news_score is None:
        return None
    return round(_clamp((float(news_score) - 50.0) / 50.0), 4)


def sector_factor(sector_rs_pct: Optional[float]) -> Optional[float]:
    """Sector rotation: the idea's sector 3-month return minus the benchmark's, squashed to
    [−1, +1] (a leading sector lifts, a lagging one trims). None → neutral."""
    if sector_rs_pct is None:
        return None
    return round(math.tanh(float(sector_rs_pct) / SECTOR_RS_SCALE), 4)


def quality_factor(quality_score: Optional[float]) -> Optional[float]:
    """Fundamental quality as a tilt: a 0–100 quality read (50 neutral) → [−1, +1], so weak-quality
    names that technicals flag get trimmed and strong ones lifted. None → neutral (e.g. price-only
    markets with no fundamentals)."""
    if quality_score is None:
        return None
    return round(_clamp((float(quality_score) - 50.0) / 50.0), 4)


def quality_score(info: Optional[dict]) -> Optional[float]:
    """A simple 0–100 fundamental-quality read (50 neutral) from a `get_stock_info` dict: rewards
    high ROE, low leverage, and healthy profit margins. Each present metric contributes a ±1 sub-
    score; the average maps to 0–100. None when no fundamentals are available (price-only markets)."""
    if not info:
        return None
    subs = []
    roe = info.get("roe")                       # fraction, e.g. 0.15 = 15%
    if roe is not None:
        subs.append(_clamp((float(roe) - 0.10) / 0.20))          # 10% neutral, ≥30% → +1
    de = info.get("debt_to_equity")             # yfinance reports this as a percent (50 = 0.5×)
    if de is not None:
        de_x = float(de) / 100.0 if float(de) > 5 else float(de)
        subs.append(_clamp((1.0 - de_x) / 1.0))                  # <1× good, ≥2× → −1
    pm = info.get("profit_margins")             # fraction
    if pm is not None:
        subs.append(_clamp((float(pm) - 0.05) / 0.15))           # 5% neutral, ≥20% → +1
    if not subs:
        return None
    return round(50.0 + (sum(subs) / len(subs)) * 50.0, 1)


# ── Composite ───────────────────────────────────────────────────────

def rank_weights() -> dict:
    """The disclosed per-factor weights (from settings) + shape constants — surfaced in the API."""
    return {
        "earnings": settings.rank_w_earnings, "news": settings.rank_w_news,
        "sector": settings.rank_w_sector, "quality": settings.rank_w_quality,
        "min_multiplier": settings.rank_min_multiplier,     # quality gate: drop below this
        "earnings_window_days": EARNINGS_WINDOW_DAYS, "sector_rs_scale": SECTOR_RS_SCALE,
    }


def composite_multiplier(factors: dict, weights: Optional[dict] = None) -> float:
    """Π (1 + wᵢ · factorᵢ) over the factor values present in `factors` (keys in FACTOR_NAMES).
    A missing/None factor or a zero weight contributes ×1, so the result is 1.0 when nothing
    applies — leaving the base ranking untouched."""
    w = weights or {"earnings": settings.rank_w_earnings, "news": settings.rank_w_news,
                    "sector": settings.rank_w_sector, "quality": settings.rank_w_quality}
    mult = 1.0
    for name in FACTOR_NAMES:
        f = factors.get(name)
        wi = w.get(name, 0.0)
        if f is not None and wi:
            mult *= (1.0 + wi * _clamp(float(f)))
    return mult


def apply(base_score: float, factors: dict, weights: Optional[dict] = None) -> float:
    """`base_score` (conviction × regime tilt) scaled by the composite factor multiplier."""
    return base_score * composite_multiplier(factors, weights)
