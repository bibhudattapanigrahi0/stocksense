"""
Idea **selection** — turn the raw long/short bot books into one small, ranked "best-of" shortlist.

The intraday bots can flag dozens of setups; a trader can act on a handful. This layer scores each
candidate on one transparent, deterministic **composite** and keeps only the top few. Three ideas,
all disclosed (nothing here is a black box — cf. `sizing.py`, `bots/ai.py`):

  1. Confidence — a **Wilson score lower bound** on the measured win rate. Unlike a raw win %, it
     is *sample-size aware*: 60% over 20 trades is trusted far less than 60% over 300, so a thin
     edge can't outrank a well-measured one. This is the statistical backbone of the ranking.
  2. Edge + agreement — the measured expectancy (`quality`) squashed to 0..1, and the number of
     agreeing bots with **diminishing returns** (the 4th bot adds far less than the 2nd).
  3. Live sentiment — per-coin news + the market-wide Fear & Greed gauge, combined into an
     alignment in [-1, +1] and applied as a **soft multiplier**: sentiment that contradicts the
     setup scales its score *down* (so it rarely makes the cut) but never hard-vetoes a strong
     technical — supportive sentiment lifts it. Extremes get a contrarian nudge (chasing extreme
     greed with a fresh long, or extreme fear with a fresh short, is penalised).

Everything here is a pure function of numbers the idea already carries → trivially unit-testable
and network-free. `score_weights()` exposes the weights so the API can show its work.
"""

from __future__ import annotations

import math
from typing import Optional

# ── Composite weights (base terms sum to 1.0; sentiment is a separate ± multiplier) ──
W_EDGE = 0.40          # measured expectancy (quality)
W_CONF = 0.35          # Wilson-adjusted win rate (confidence)
W_AGREE = 0.25         # agreeing-bot count (diminishing)
W_SENT = 0.35          # max ± swing sentiment applies to the base score (soft, not a veto)

# ── Sub-term shape constants ──
Z_WILSON = 1.64        # ~90% one-sided confidence for the Wilson lower bound
UNPROVEN_WIN_PCT = 45.0   # confidence for an idea with no measured sample (< a proven 50%+)
EDGE_SCALE = 0.5       # expectancy %-per-trade that maps to a strong edge term
AGREE_DECAY = 0.55     # agreement_term = 1 − AGREE_DECAY**conviction (diminishing returns)
NEWS_W, MACRO_W = 0.6, 0.4     # per-coin news vs market-wide weight inside sentiment alignment
EXTREME_GREED, EXTREME_FEAR = 85, 15   # Fear & Greed levels where sentiment turns contrarian


def score_weights() -> dict:
    """The disclosed composite weights + key constants — surfaced in the API response."""
    return {
        "edge": W_EDGE, "confidence": W_CONF, "agreement": W_AGREE, "sentiment_max": W_SENT,
        "wilson_z": Z_WILSON, "news_weight": NEWS_W, "macro_weight": MACRO_W,
    }


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── 1. Confidence: sample-size-aware win rate ───────────────────────


def wilson_lower_bound(win_rate_pct: Optional[float], n: Optional[int], z: float = Z_WILSON) -> float:
    """Wilson score interval **lower** bound for a win rate, as a percentage.

    Shrinks an observed win rate toward pessimism in proportion to how little data backs it, so a
    small sample can't masquerade as a reliable edge. With no measured sample (`n` < 1) there is
    nothing to trust yet → a conservative `UNPROVEN_WIN_PCT`.
    """
    if win_rate_pct is None or n is None or n < 1:
        return UNPROVEN_WIN_PCT
    p = _clamp(float(win_rate_pct) / 100.0)
    n = float(n)
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return round(_clamp((center - margin) / denom) * 100.0, 1)


def confidence_term(conf_win_pct: float) -> float:
    """Confidence sub-term in 0..1 — just the Wilson win rate on a 0..1 scale."""
    return _clamp(conf_win_pct / 100.0)


# ── 2. Edge + agreement ─────────────────────────────────────────────


def edge_term(quality: Optional[float], r_multiple: Optional[float]) -> float:
    """Edge sub-term in 0..1. With a measured expectancy (`quality`, %-per-trade) squash it with a
    logistic centred at 0 (break-even → 0.5). With no measured edge, sit just below neutral and let
    reward:risk nudge it — an unproven setup shouldn't score like a proven one."""
    if quality is not None:
        return round(1.0 / (1.0 + math.exp(-float(quality) / EDGE_SCALE)), 4)
    if r_multiple:
        return round(0.40 + 0.20 * _clamp((float(r_multiple) - 1.0) / 2.0), 4)   # R 1→0.40, ≥3→0.60
    return 0.40


def agreement_term(conviction: int) -> float:
    """Agreement sub-term in 0..1 with diminishing returns: 1×→0.45, 2×→0.70, 3×→0.83, 4×→0.91."""
    return round(1.0 - AGREE_DECAY ** max(0, int(conviction)), 4)


# ── 3. Live sentiment alignment ─────────────────────────────────────


def sentiment_alignment(side: str, news_score: Optional[float], fng_value: Optional[float]) -> float:
    """How much live sentiment *supports* this idea's direction, in [-1, +1].

    `news_score` (per-coin, 0-100, 50 neutral) and `fng_value` (market Fear & Greed, 0-100) are
    each sign-aligned to the side (bullish news / greed support longs; bearish / fear support
    shorts), then blended. At the extremes the macro term flips **contrarian**: chasing extreme
    greed with a fresh long — or extreme fear with a fresh short — is penalised. Missing inputs
    contribute nothing (neutral)."""
    sign = 1.0 if side == "long" else -1.0

    news_align = 0.0 if news_score is None else sign * (_clamp(float(news_score), 0, 100) - 50.0) / 50.0

    if fng_value is None:
        macro_align = 0.0
    else:
        fng = _clamp(float(fng_value), 0, 100)
        if fng >= EXTREME_GREED:          # extreme greed → fade fresh longs, favour shorts
            macro_align = -sign * (fng - EXTREME_GREED) / (100.0 - EXTREME_GREED)
        elif fng <= EXTREME_FEAR:         # extreme fear → favour longs, fade fresh shorts
            macro_align = sign * (EXTREME_FEAR - fng) / EXTREME_FEAR
        else:                             # normal band → momentum: greed helps longs, fear shorts
            macro_align = sign * (fng - 50.0) / 50.0

    return round(_clamp(NEWS_W * news_align + MACRO_W * macro_align, -1.0, 1.0), 4)


# ── Composite + scoring one candidate ───────────────────────────────


def composite_score(edge: float, conf: float, agree: float, alignment: float) -> float:
    """Blend the base sub-terms then apply sentiment as a bounded ± multiplier. In 0..1.

    base = W_EDGE·edge + W_CONF·conf + W_AGREE·agree      (a pure technical/edge quality score)
    score = base · (1 + W_SENT·alignment)                 (soft down-/up-rank; never a hard veto)
    """
    base = W_EDGE * edge + W_CONF * conf + W_AGREE * agree
    return round(_clamp(base * (1.0 + W_SENT * alignment)), 4)


def score_candidate(
    *, side: str, conviction: int, quality: Optional[float], r_multiple: Optional[float],
    est_win_pct: Optional[float], trades: Optional[int],
    news_score: Optional[float] = None, fng_value: Optional[float] = None,
) -> dict:
    """Score one idea into the fields the shortlist ranks on. Pure — everything is passed in.

    Returns `conf_win_pct` (Wilson), the three sub-terms, the sentiment `alignment`, and the final
    `selection_score`, plus a compact `terms` breakdown for the "why" card / debugging.
    """
    conf_win = wilson_lower_bound(est_win_pct, trades)
    e = edge_term(quality, r_multiple)
    c = confidence_term(conf_win)
    a = agreement_term(conviction)
    align = sentiment_alignment(side, news_score, fng_value)
    return {
        "conf_win_pct": conf_win,
        "alignment": align,
        "selection_score": composite_score(e, c, a, align),
        "terms": {"edge": e, "confidence": c, "agreement": a, "sentiment": align},
    }


# ── Curation: rank → floor → cap ────────────────────────────────────


def curate(candidates: list[dict], top_k: int, min_score: float = 0.0) -> tuple[list[dict], dict]:
    """Rank scored candidates by `selection_score`, drop those below `min_score`, keep the top
    `top_k` (long + short mixed). Returns `(shortlist, diagnostics)`."""
    ranked = sorted(
        candidates,
        key=lambda i: (i.get("selection_score") or 0.0, i.get("conf_win_pct") or 0.0,
                       i.get("conviction") or 0, i.get("r_multiple") or 0.0),
        reverse=True,
    )
    kept = [i for i in ranked if (i.get("selection_score") or 0.0) >= min_score][:top_k]
    return kept, {
        "considered": len(candidates), "kept": len(kept),
        "cut": len(candidates) - len(kept), "top_k": top_k, "min_score": min_score,
    }
