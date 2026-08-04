"""
Position sizing — turn a trade idea's measured edge (win probability + reward:risk) into a
concrete **risk budget** and **portfolio allocation**, using capped fractional Kelly.

Kelly's f* = p − (1−p)/b is the fraction of equity to *risk* (i.e. lose if stopped) on a bet
that pays b:1 with win probability p. Full Kelly is famously too volatile for real trading, so
we scale it by `kelly_fraction` (¼ by default), cap any single idea's risk, then normalize a
*book* of simultaneous ideas to a total risk budget. Allocation (notional %) follows from the
risk and the stop distance:

    alloc = risk_pct / stop_distance_pct

(risking r% behind a stop s% away means committing r/s of capital), capped per idea and
normalized so the book never allocates more than 100%.

Every idea is sized from numbers it already carries — win prob (`est_win_pct` / `avg_win_rate`
/ `calibrated_win_pct`), reward:risk (`r_multiple`, or the entry/stop/target geometry), and the
stop distance — so this stays a set of pure, I/O-free functions shared by both the intraday
(`bots/ideas.py`) and daily (`backtest/runner.py`) idea paths.

When an idea has no measured win probability yet (no backtest), sizing degrades gracefully to
fixed-fractional risk scaled by conviction — mirroring how `ideas.py` already falls back to a
neutral vote weight without an edge table.
"""

from __future__ import annotations

from typing import Optional

from pipeline_core.config import settings

# Keys the sizer writes onto each idea (documented so callers/tests can rely on them).
SIZE_KEYS = ("risk_pct", "alloc_pct", "kelly_full", "kelly_used", "size_note")


def _num(x) -> Optional[float]:
    """float or None (NaN-safe)."""
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _prob(x) -> Optional[float]:
    """A win probability as a fraction in [0, 1]. Accepts a percentage (58.0) or a fraction
    (0.58); returns None if missing or out of range."""
    v = _num(x)
    if v is None:
        return None
    if v > 1.0:  # given as a percentage
        v = v / 100.0
    return v if 0.0 <= v <= 1.0 else None


def _first_num(idea: dict, *keys: str) -> Optional[float]:
    """First key that yields a real number — lets one sizer serve idea dicts with different
    field names (intraday `entry`/`stop`/`r_multiple`, daily `price`/`stop_price`/`risk_reward`)."""
    for k in keys:
        v = _num(idea.get(k))
        if v is not None:
            return v
    return None


def kelly_fraction(win_prob, reward_risk) -> float:
    """Full-Kelly *risk* fraction f* = p − (1−p)/b, clamped to [0, 1].

    Returns 0.0 for a non-positive edge or invalid/missing inputs (so a losing or unknown
    setup is never sized up)."""
    p = _prob(win_prob)
    b = _num(reward_risk)
    if p is None or b is None or b <= 0:
        return 0.0
    f = p - (1.0 - p) / b
    return max(0.0, min(1.0, f))


def _reward_risk(idea: dict) -> Optional[float]:
    """Reward:risk (b) for an idea — a stored `r_multiple`/`reward_risk`/`risk_reward` if present,
    else derived from entry/stop/target geometry (respecting side)."""
    b = _first_num(idea, "reward_risk", "r_multiple", "risk_reward")
    if b is not None and b > 0:
        return b
    entry = _first_num(idea, "entry", "price")
    stop = _first_num(idea, "stop", "stop_price")
    target = _first_num(idea, "target", "target_price")
    if None in (entry, stop, target) or entry <= 0:
        return None
    if idea.get("side") == "short":
        risk, reward = stop - entry, entry - target
    else:
        risk, reward = entry - stop, target - entry
    return (reward / risk) if risk > 0 and reward > 0 else None


def _stop_distance_pct(idea: dict) -> Optional[float]:
    """Stop distance as a fraction of entry. Uses a stored `stop_pct` (daily ideas carry it),
    else |entry − stop| / entry. Accepts a percentage stop_pct (>1 → /100)."""
    sp = _first_num(idea, "stop_pct")
    if sp is not None and sp > 0:
        return sp / 100.0 if sp > 1.0 else sp
    entry = _first_num(idea, "entry", "price")
    stop = _first_num(idea, "stop", "stop_price")
    if entry is None or stop is None or entry <= 0:
        return None
    d = abs(entry - stop) / entry
    return d if d > 0 else None


def _win_prob(idea: dict) -> Optional[float]:
    """The idea's win probability from the best field it carries: a learned-fusion probability first,
    then the isotonic-calibrated rate, then any explicit/advertised win-rate."""
    for k in ("model_win_pct", "calibrated_win_pct", "win_prob", "est_win_pct", "avg_win_rate"):
        p = _prob(idea.get(k))
        if p is not None:
            return p
    return None


def sizing_params() -> dict:
    """The active sizing configuration — attached to idea payloads so the UI/consumer can show
    what the numbers mean."""
    return {
        "enabled": settings.sizing_enabled,
        "method": "quarter_kelly" if settings.sizing_kelly_fraction == 0.25 else "fractional_kelly",
        "kelly_fraction": settings.sizing_kelly_fraction,
        "max_position_pct": round(settings.sizing_max_position_pct * 100, 1),
        "risk_per_trade_pct": round(settings.sizing_risk_per_trade_pct * 100, 2),
        "max_book_risk_pct": round(settings.sizing_max_book_risk_pct * 100, 2),
    }


def size_book(
    ideas: list[dict],
    *,
    kelly_frac: Optional[float] = None,
    max_position_pct: Optional[float] = None,
    risk_per_trade_pct: Optional[float] = None,
    max_book_risk_pct: Optional[float] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Size a book of simultaneous ideas in place, writing `SIZE_KEYS` onto each, and return a
    book summary.

    Steps: capped fractional Kelly → per-idea risk (fallback: conviction-scaled fixed fraction)
    → normalize total risk to `max_book_risk_pct` → allocation = risk / stop-distance, capped at
    `max_position_pct` and normalized to ≤100%. Unset args fall back to settings, so callers just
    call `size_book(ideas)`; tests pass explicit params. Returns {sized, total_risk_pct,
    total_alloc_pct, ...params}.
    """
    kf = settings.sizing_kelly_fraction if kelly_frac is None else kelly_frac
    max_pos = settings.sizing_max_position_pct if max_position_pct is None else max_position_pct
    risk_cap = settings.sizing_risk_per_trade_pct if risk_per_trade_pct is None else risk_per_trade_pct
    book_cap = settings.sizing_max_book_risk_pct if max_book_risk_pct is None else max_book_risk_pct
    on = settings.sizing_enabled if enabled is None else enabled

    if not on or not ideas:
        return {"enabled": bool(on), "sized": 0, "total_risk_pct": 0.0, "total_alloc_pct": 0.0}

    # 1) Raw, *uncapped* per-idea risk weight from fractional Kelly (edge-proportional), or a
    #    conviction-scaled fallback when there's no measured edge. Left uncapped here so that
    #    normalizing to the book budget (step 2) preserves the relative edge ordering before the
    #    per-trade safety cap bites (step 3) — otherwise every strong idea flattens to the cap.
    raw: list[float] = []
    for idea in ideas:
        p, b = _win_prob(idea), _reward_risk(idea)
        f_full = kelly_fraction(p, b) if (p is not None and b is not None) else None
        if f_full is not None:
            w = kf * f_full
            note = f"{kf:g}×Kelly"
        else:
            conv = max(1, int(_num(idea.get("conviction")) or 1))
            w = risk_cap * min(1.0, conv / 3.0)
            note = "fixed-fractional (no measured edge)"
        idea["kelly_full"] = round(f_full, 4) if f_full is not None else None
        idea["size_note"] = note
        raw.append(max(0.0, w))

    # 2) Normalize the book's total risk down to the budget (preserve proportions; never inflate).
    total_raw = sum(raw)
    scale = (book_cap / total_raw) if total_raw > book_cap and total_raw > 0 else 1.0

    # 3) Apply the per-trade cap *after* scaling, then derive allocation from the stop distance.
    allocs: list[float] = []
    for idea, w in zip(ideas, raw):
        r = min(w * scale, risk_cap)
        idea["risk_pct"] = round(r * 100, 2)
        idea["kelly_used"] = round(r, 4)
        sd = _stop_distance_pct(idea)
        allocs.append(min(r / sd, max_pos) if sd else 0.0)

    # 4) Normalize allocations to ≤100% (leave cash if the book is light; don't force deployment).
    total_a = sum(allocs)
    a_scale = (1.0 / total_a) if total_a > 1.0 and total_a > 0 else 1.0
    total_risk = total_alloc = 0.0
    for idea, a in zip(ideas, allocs):
        a2 = a * a_scale
        idea["alloc_pct"] = round(a2 * 100, 1)
        total_risk += idea["risk_pct"]
        total_alloc += a2

    return {
        "enabled": True,
        "sized": len(ideas),
        "total_risk_pct": round(total_risk, 2),
        "total_alloc_pct": round(total_alloc * 100, 1),
        "kelly_fraction": kf,
        "max_position_pct": round(max_pos * 100, 1),
        "risk_per_trade_pct": round(risk_cap * 100, 2),
        "max_book_risk_pct": round(book_cap * 100, 2),
    }
