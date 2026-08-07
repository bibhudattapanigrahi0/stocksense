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


# ── Multi-pillar confluence (breadth across independent signal families) ──
# Conviction measures DEPTH within one family (how many technical strategies agree in a horizon). This
# measures BREADTH: how many *independent* families confirm the idea — the technical setup itself plus
# fundamental quality, institutional accumulation, a regime tailwind, bullish news, and sector leadership.
# Ideas clearing enough pillars are "actionable" (a small, high-conviction set); the rest are a watchlist.
# Pure — reads only fields `enrich_ideas_live` already attached, so it's network-free and unit-testable.

PILLAR_NAMES = ("technical", "quality", "smart_money", "regime", "sentiment", "sector")
QUALITY_PILLAR_MIN = 55.0     # quality_score (0–100, 50 neutral) ≥ this ⇒ a supporting quality pillar
NEWS_PILLAR_MIN = 55.0        # news_score (0–100, 50 neutral) ≥ this ⇒ a supporting sentiment pillar


def _smart_money_supportive(sig: Optional[dict]) -> Optional[bool]:
    """True when the smart money is ACCUMULATING — the market-agnostic read on the normalized
    smart-money signal (services/smart_money.py): 13F QoQ additions (US), COT commercials
    net-long (metals), exchange outflows / whale accumulation (crypto), bulk-deal buys (IN).
    False when it's measurably distributing; None when it can't be judged — e.g. a yfinance
    ownership *level* with no delta, or no signal at all. Only genuine accumulation supports the pillar."""
    if not sig:
        return None
    direction = sig.get("direction")
    if direction is not None:
        return direction == "accumulate"
    # Back-compat: a raw ownership dict (no normalized direction) → judge by the QoQ delta.
    qoq = sig.get("net_delta_pct", sig.get("qoq_change_pct"))
    return float(qoq) > 0 if qoq is not None else None


def idea_pillars(c: dict) -> dict:
    """Which independent families support this idea: True (supports), False (measured, doesn't), or None
    (not available this market). Pure function of numbers the candidate already carries post-enrichment."""
    qs = c.get("quality_score")
    ns = c.get("news_score")
    rs = c.get("sector_rs")
    rf = c.get("regime_fit")
    return {
        "technical": (c.get("eligible_conviction", c.get("conviction", 0)) or 0) >= 1,
        "quality": (qs >= QUALITY_PILLAR_MIN) if qs is not None else None,
        "smart_money": _smart_money_supportive(c.get("smart_money") or c.get("inst_ownership")),
        "regime": (rf == "tailwind") if rf is not None else None,
        "sentiment": (ns >= NEWS_PILLAR_MIN) if ns is not None else None,
        "sector": (rs > 0) if rs is not None else None,
    }


def pillar_summary(c: dict, available: Optional[int] = None) -> dict:
    """`idea_pillars` plus the count of supporting families and how many were available to confirm.
    `available` (families switched on for this market, from the caller) defaults to the number of
    non-None readings on this idea when not supplied (standalone/testing use)."""
    p = idea_pillars(c)
    supporting = [k for k, v in p.items() if v is True]
    avail = available if available is not None else sum(1 for v in p.values() if v is not None)
    return {"pillars": p, "supporting": supporting, "count": len(supporting), "available": avail}


def pillar_tier(count: int, available: int, min_actionable: Optional[int] = None) -> str:
    """'actionable' when an idea clears the confluence bar, else 'watch'. The bar is clamped to the
    families actually available this market — min(min_actionable, available) — so thin-pillar markets
    (e.g. crypto in a neutral tide: only the technical family) still surface actionable ideas rather
    than filing everything under watch."""
    thr = settings.pillar_min_actionable if min_actionable is None else min_actionable
    thr = max(1, min(thr, available or 1))
    return "actionable" if count >= thr else "watch"


def pillar_disclosure() -> dict:
    """The confluence config, surfaced in the ideas payload so the UI can explain the Actionable/Watchlist
    split (mirrors `rank_weights` for the composite banner)."""
    return {
        "enabled": settings.pillar_tiering_enabled,
        "min_actionable": settings.pillar_min_actionable,
        "names": list(PILLAR_NAMES),
        "quality_min": QUALITY_PILLAR_MIN, "news_min": NEWS_PILLAR_MIN,
    }


# ── Realized-lift attribution (does each factor actually earn its weight?) ──
# The soft factors above re-rank the board, but until now nothing measured whether they *help*. Each
# tracked idea records the factor values it was generated with; once it closes it also carries a
# realized return. This splits the closed ledger by each factor's sign at entry and compares realized
# win-rates — turning "we added news sentiment" into "news added +N pp" (or "−0, drop it").

_ATTRIB_EPS = 0.05      # |factor| must exceed this to count as supportive/adverse (ignore ~neutral)


def _cohort_stats(returns: list) -> dict:
    """Win-rate + average realized return (%) over a list of realized returns. A flat (==0) trade is
    counted in the sample and the average but is neither a win nor a loss for the rate."""
    wins = sum(1 for r in returns if r > 0)
    losses = sum(1 for r in returns if r < 0)
    decided = wins + losses
    return {
        "n": len(returns),
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "avg_return": round(sum(returns) / len(returns), 2) if returns else None,
    }


def _lift(sup: dict, adv: dict, min_samples: int) -> Optional[float]:
    """Supportive − adverse win-rate (pp), only when BOTH cohorts have the evidence to compare."""
    if sup["n"] < min_samples or adv["n"] < min_samples:
        return None
    if sup["win_rate"] is None or adv["win_rate"] is None:
        return None
    return round(sup["win_rate"] - adv["win_rate"], 1)


def factor_attribution(closed: list, *, min_samples: Optional[int] = None,
                       eps: float = _ATTRIB_EPS) -> dict:
    """Realized win-rate lift per soft factor, measured over CLOSED tracked ideas.

    For each of the four signed factors we split the closed set into a SUPPORTIVE cohort (factor
    > +eps at entry) and an ADVERSE cohort (< −eps) and report
    ``lift = supportive_win_rate − adverse_win_rate`` (pp) — positive ⇒ the factor pointed the right
    way. Regime is attributed by its tailwind/headwind label. A lift is reported only when BOTH
    cohorts reach ``min_samples`` (the closed set splits several ways, so we refuse to over-read a
    handful of trades); otherwise ``lift=None`` with ``status="insufficient"``.

    Pure — reads only fields already on the ledger (``factors`` + ``realized_return_pct``). Ideas
    tracked before attribution existed have no ``factors`` block and are absent from every cohort."""
    thr = settings.attribution_min_samples if min_samples is None else min_samples
    withf = [c for c in closed
             if c.get("factors") and c.get("realized_return_pct") is not None]
    baseline = _cohort_stats([c["realized_return_pct"] for c in withf])

    factors: dict[str, dict] = {}
    for name in FACTOR_NAMES:                                  # earnings / news / sector / quality
        sup, adv = [], []
        for c in withf:
            f = c["factors"].get(name)
            if f is None:
                continue
            (sup if f > eps else adv if f < -eps else []).append(c["realized_return_pct"])
        s, a = _cohort_stats(sup), _cohort_stats(adv)
        lift = _lift(s, a, thr)
        factors[name] = {"supportive": s, "adverse": a, "lift": lift,
                         "status": "ok" if lift is not None else "insufficient"}

    # Regime fit is a label, not a signed number — attribute tailwind vs headwind directly.
    rs = _cohort_stats([c["realized_return_pct"] for c in withf
                        if c["factors"].get("regime_fit") == "tailwind"])
    ra = _cohort_stats([c["realized_return_pct"] for c in withf
                        if c["factors"].get("regime_fit") == "headwind"])
    r_lift = _lift(rs, ra, thr)
    factors["regime"] = {"supportive": rs, "adverse": ra, "lift": r_lift,
                         "status": "ok" if r_lift is not None else "insufficient"}

    return {"sample": len(withf), "baseline": baseline, "factors": factors, "min_samples": thr}
