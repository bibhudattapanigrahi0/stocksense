"""
Curated **basket** — distil the full 30-idea daily board into ONE small, deployable best-of.

The daily Trade Ideas board surfaces up to 30 ideas (10 × short/medium/long). A real user can't take
30 trades, and the board sizes/de-dups each horizon *in isolation*, so there's no single "take these
four, at these weights" answer. This module is the daily analog of the intraday shortlist
(`bots/selection.py`): it pools every horizon's (already gated + intra-bucket de-duped) candidates and
picks a **cross-horizon best-of** of 3–5, sized as ONE ¼-Kelly book so allocations sum to ≤100%.

Balanced / risk-adjusted, in three disclosed steps (nothing here is a black box — everything is a pure
function of numbers the idea already carries, so it's network-free and trivially unit-testable):

  1. Score — each idea scores on a transparent composite (weights in `settings.basket_w_*`):
       score = w_ev·EV + w_conf·confidence + w_quality·rank
     • EV         — expected return %, ``p·target − (1−p)·stop`` (p = the idea's best win prob:
                    learned-fusion → calibrated → advertised, via `sizing._win_prob`), logistic-squashed.
     • confidence — sample/agreement-aware trust. Daily ideas have no clean per-idea trade count, so we
                    use strategy AGREEMENT (diminishing returns, mirroring `selection.agreement_term`),
                    lifted when a calibrated / learned-fusion probability backs the win rate. The
                    "balanced" lever — a thinly-agreed idea can't outrank a well-backed one on EV alone.
     • rank       — the idea's existing `rank_score` (conviction × regime × soft factors), normalized
                    per-horizon so a 3-conviction long isn't unfairly beaten by a 5-conviction short.

  2. Select — GREEDY + DIVERSIFIED (not just "top N"): take the best, then repeatedly take the best
     remaining that is NOT in the same correlation cluster as one already chosen (hard, reusing the
     board's cross-horizon dedup clusters), with a soft per-sector cap and a soft horizon-spread nudge.
     The basket **size is regime-scaled**: more names in risk-on, fewer in risk-off.

  3. Stick — hysteresis so the basket is actually holdable. A still-valid prior member (present on
     today's board, not stopped-out / at-target) carries a score bonus, so a challenger only displaces
     it when it's *materially* better (raw score beats the incumbent's by > `basket_hysteresis_margin`).
     A broken incumbent gets no bonus and drops out naturally.

Sizing across the picks reuses `sizing.size_book` (with the regime risk-off haircut, like
`runner._size_bucket`); the whole thing operates on COPIES of the board candidates so the per-bucket
board below is left exactly as-is. Expected-return numbers are estimates surfaced for comparison, not
advice.
"""

from __future__ import annotations

import math
from copy import copy
from datetime import datetime, timezone
from typing import Optional

from pipeline_core import sizing
from pipeline_core.config import settings

# ── Sub-term shape constants (weights themselves are in settings.basket_w_*) ──
EV_SCALE = 3.0            # expected-return %-per-trade that maps to a strong EV term (logistic centre 0)
AGREE_DECAY = 0.55        # confidence base = 1 − AGREE_DECAY**conviction (mirrors selection.agreement_term)
MODEL_TRUST = 0.15        # confidence lift when a learned-fusion win prob backs the idea
CAL_TRUST = 0.08          # …or (smaller) when an isotonic-calibrated win rate backs it
TIE_EPS = 0.02            # score tie-band within which the soft horizon-spread nudge applies
_BROKEN_ZONES = {"below_stop", "at_target"}     # an incumbent here has hit its plan → not carried
# Hold-mode score tilt by horizon — favours longer holds (short-term setups churn fastest), so a committed
# hold book leans to names with staying power. ×1 everywhere reproduces the daily basket ordering.
HOLD_HORIZON_FACTOR = {"short": 0.8, "medium": 1.0, "long": 1.2}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── Disclosure ──────────────────────────────────────────────────────

def basket_weights() -> dict:
    """The disclosed composite weights (normalized to sum to 1) + shape constants — surfaced in the API."""
    w_ev, w_conf, w_q = settings.basket_w_ev, settings.basket_w_confidence, settings.basket_w_quality
    tot = (w_ev + w_conf + w_q) or 1.0
    return {
        "ev": round(w_ev / tot, 3), "confidence": round(w_conf / tot, 3), "quality": round(w_q / tot, 3),
        "ev_scale": EV_SCALE, "hysteresis_margin": settings.basket_hysteresis_margin,
    }


def basket_params() -> dict:
    """The active basket configuration — attached to the payload so the UI can show what it means."""
    return {
        "enabled": settings.basket_enabled,
        "size_min": settings.basket_size_min, "size_max": settings.basket_size_max,
        "sector_cap": settings.basket_sector_cap,
        "hysteresis_margin": settings.basket_hysteresis_margin,
    }


# ── Per-idea numbers ────────────────────────────────────────────────

def _target_stop_pct(idea: dict) -> tuple[Optional[float], Optional[float]]:
    """(target_pct, stop_pct) as positive fractions — stored values if present, else price geometry."""
    tp, sp = idea.get("target_pct"), idea.get("stop_pct")
    tp = float(tp) if tp is not None else None
    sp = float(sp) if sp is not None else None
    entry = idea.get("price")
    if (tp is None or sp is None) and entry:
        if sp is None and idea.get("stop_price") is not None:
            sp = abs(entry - idea["stop_price"]) / entry
        if tp is None and idea.get("target_price") is not None:
            tp = abs(idea["target_price"] - entry) / entry
    return (tp if tp and tp > 0 else None, sp if sp and sp > 0 else None)


def expected_value(win_prob: Optional[float], target_pct: Optional[float],
                   stop_pct: Optional[float]) -> Optional[float]:
    """Expected return as a fraction: ``p·target − (1−p)·stop``. None if inputs are missing — an idea
    with no win prob or no plan geometry has no computable EV (it falls back to a neutral term)."""
    if win_prob is None or target_pct is None or stop_pct is None:
        return None
    return win_prob * target_pct - (1.0 - win_prob) * stop_pct


def _win_prob_source(idea: dict) -> Optional[str]:
    """Which field the win prob came from (for the 'why' card) — same precedence as sizing._win_prob."""
    for k, src in (("model_win_pct", "learned"), ("calibrated_win_pct", "calibrated"),
                   ("win_prob", "measured"), ("est_win_pct", "measured"), ("avg_win_rate", "advertised")):
        if sizing._prob(idea.get(k)) is not None:
            return src
    return None


def ev_term(ev: Optional[float]) -> float:
    """EV sub-term in 0..1: a logistic on the expected return % (break-even → 0.5). No EV → 0.40
    (just below neutral — an idea we can't price for edge shouldn't score like a proven one)."""
    if ev is None:
        return 0.40
    return round(1.0 / (1.0 + math.exp(-(ev * 100.0) / EV_SCALE)), 4)


def confidence_term(conviction: Optional[int], *, has_model: bool = False,
                    has_calibration: bool = False) -> float:
    """Confidence sub-term in 0..1. Base is strategy agreement with diminishing returns
    (1×→0.45, 2×→0.70, 3×→0.83), lifted when a learned/calibrated probability backs the win rate."""
    conv = max(0, int(conviction or 0))
    base = 1.0 - AGREE_DECAY ** conv
    if has_model:
        base += MODEL_TRUST
    elif has_calibration:
        base += CAL_TRUST
    return round(_clamp(base), 4)


def score_candidate(idea: dict, rank_norm: float, weights: Optional[dict] = None) -> dict:
    """Score one (copied) candidate, writing the composite + its breakdown onto it. Pure.

    Returns the same dict, now carrying `win_prob` (fraction), `win_prob_source`, `expected_return_pct`,
    `basket_score`, and a `basket_terms` breakdown for the 'why' card."""
    w = weights or basket_weights()
    p = sizing._win_prob(idea)
    src = _win_prob_source(idea)                # read the source BEFORE we write win_prob onto the idea
    tp, sp = _target_stop_pct(idea)
    ev = expected_value(p, tp, sp)
    e = ev_term(ev)
    c = confidence_term(idea.get("eligible_conviction", idea.get("conviction")),
                        has_model=idea.get("model_win_pct") is not None,
                        has_calibration=idea.get("calibrated_win_pct") is not None)
    q = _clamp(rank_norm)
    score = round(w["ev"] * e + w["confidence"] * c + w["quality"] * q, 4)
    idea["win_prob"] = round(p * 100, 1) if p is not None else None
    idea["win_prob_source"] = src
    idea["expected_return_pct"] = round(ev * 100, 2) if ev is not None else None
    idea["basket_score"] = score
    idea["basket_terms"] = {"ev": e, "confidence": c, "quality": round(q, 4)}
    return idea


# ── Pool + score the whole board ────────────────────────────────────

def _pool(ideas: dict, side: str = "long") -> list[dict]:
    """Flatten every horizon's shown candidates into one list of COPIES, each tagged with its
    `horizon`/`horizon_label` (copies so basket sizing never mutates the board candidates below).
    `side="long"` (default) keeps long ideas only — the curated/coattail/hold baskets are long books;
    `side="short"` keeps shorts only — the sell-side coattail SHORT sleeve (crypto)."""
    want_short = side == "short"
    pool: list[dict] = []
    buckets = ideas.get("buckets") or {}
    for h in ideas.get("horizons", []):
        b = buckets.get(h) or {}
        label = b.get("label", h)
        for c in b.get("candidates", []):
            if (c.get("side") == "short") != want_short:
                continue                     # keep only the requested direction
            d = copy(c)
            d["horizon"] = h
            d["horizon_label"] = label
            pool.append(d)
    return pool


def _attach_scores(pool: list[dict]) -> None:
    """Normalize each idea's rank_score within its horizon (÷ bucket max) then score the pool.
    A missing rank_score falls back to conviction so the base board (no enrichment) still ranks."""
    weights = basket_weights()
    by_h: dict[str, float] = {}
    for c in pool:
        base = c.get("rank_score", c.get("conviction", 0)) or 0.0
        by_h[c["horizon"]] = max(by_h.get(c["horizon"], 0.0), float(base))
    for c in pool:
        base = float(c.get("rank_score", c.get("conviction", 0)) or 0.0)
        hmax = by_h.get(c["horizon"], 0.0)
        rank_norm = (base / hmax) if hmax > 0 else 0.5
        score_candidate(c, rank_norm, weights)


# ── Selection: greedy, diversified, hysteresis-aware ────────────────

def _basket_size(regime: Optional[dict], hold_mode: bool = False, invert: bool = False) -> int:
    """Regime-scaled target size: risk-on → max, risk-off → min, neutral/offline → the midpoint.
    Hold mode uses the (typically tighter) hold-book size range. `invert` flips risk-on/off for a SHORT
    book — a risk-OFF tide is a tailwind for shorts, so it should surface MORE of them, not fewer."""
    lo = settings.hold_size_min if hold_mode else settings.basket_size_min
    hi = settings.hold_size_max if hold_mode else settings.basket_size_max
    state = (regime or {}).get("state")
    if invert:
        state = "risk-on" if state == "risk-off" else "risk-off" if state == "risk-on" else state
    if state == "risk-on":
        return hi
    if state == "risk-off":
        return lo
    return (lo + hi) // 2


def greedy_select(pool: list[dict], clusters: Optional[dict], *, size: int,
                  sector_cap: int, effective_score) -> list[dict]:
    """Greedy diversified pick from a scored pool. `effective_score(idea)` supplies the ranking value
    (raw basket_score, or hysteresis-boosted for incumbents). Rules:
      • correlation cluster — HARD: never two names from the same cluster (reuses the board's clusters);
      • sector cap — SOFT: defer a name over the per-sector cap, use it only if we can't otherwise fill;
      • horizon spread — SOFT: within a score tie-band, prefer an as-yet-unrepresented horizon.
    """
    remaining = sorted(pool, key=effective_score, reverse=True)
    picks: list[dict] = []
    used_clusters: set = set()
    sector_counts: dict = {}
    horizons_used: set = set()

    def _cluster_ok(c: dict) -> bool:
        cid = clusters.get(c["ticker"]) if clusters else None
        return cid is None or cid not in used_clusters

    def _sector_ok(c: dict) -> bool:
        sec = c.get("sector")
        return not (sector_cap and sec and sector_counts.get(sec, 0) >= sector_cap)

    while remaining and len(picks) < size:
        eligible = [c for c in remaining if _cluster_ok(c)]      # hard cluster de-dup
        if not eligible:
            break
        preferred = [c for c in eligible if _sector_ok(c)] or eligible   # soft sector cap (relax if forced)
        top = effective_score(preferred[0])
        band = [c for c in preferred if effective_score(c) >= top - TIE_EPS]
        # soft horizon spread within the tie-band: unrepresented horizon first, then higher score
        band.sort(key=lambda c: (c.get("horizon") in horizons_used, -effective_score(c)))
        chosen = band[0]
        picks.append(chosen)
        remaining.remove(chosen)
        cid = clusters.get(chosen["ticker"]) if clusters else None
        if cid is not None:
            used_clusters.add(cid)
        sec = chosen.get("sector")
        if sec:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        horizons_used.add(chosen.get("horizon"))
    return picks


def _incumbent_keys(prev_basket: Optional[dict], pool: list[dict]) -> set:
    """(ticker, horizon) of prior basket members still VALID today: present on the board and not
    stopped-out / at-target. These get the hysteresis bonus; broken ones drop out with no bonus."""
    if not prev_basket:
        return set()
    live = {(c["ticker"], c["horizon"]): c for c in pool}
    keys = set()
    for it in prev_basket.get("items", []):
        k = (it.get("ticker"), it.get("horizon"))
        c = live.get(k)
        if c is not None and c.get("zone") not in _BROKEN_ZONES:
            keys.add(k)
    return keys


# ── Sizing + summary ────────────────────────────────────────────────

def _size_basket(picks: list[dict], tilt: float) -> dict:
    """Size the picks as ONE ¼-Kelly book, applying the regime risk-off haircut (mirrors
    runner._size_bucket): when the tide is against longs (tilt<0) the book risk budget is trimmed."""
    if tilt < 0:
        factor = max(settings.regime_size_floor, 1 + settings.regime_size_weight * tilt)
        summary = sizing.size_book(picks, max_book_risk_pct=settings.sizing_max_book_risk_pct * factor)
        summary["regime_size_factor"] = round(factor, 3)
    else:
        summary = sizing.size_book(picks)
    return summary


def _summary(picks: list[dict], size: int) -> dict:
    """Basket-level roll-up: expected return on capital (alloc-weighted, incl. cash drag), alloc-weighted
    win prob, aggregate reward:risk, exposure vs cash, diversification, and horizon/flag tallies."""
    total_alloc = sum((c.get("alloc_pct") or 0.0) for c in picks)
    exp_ret = sum((c.get("alloc_pct") or 0.0) / 100.0 * (c.get("expected_return_pct") or 0.0)
                  for c in picks)
    wp = [(c.get("alloc_pct") or 0.0, c["win_prob"]) for c in picks if c.get("win_prob") is not None]
    wsum = sum(a for a, _ in wp)
    avg_win = round(sum(a * p for a, p in wp) / wsum, 1) if wsum > 0 else None
    rr = [c["risk_reward"] for c in picks if c.get("risk_reward") is not None]
    horizon_mix: dict = {}
    for c in picks:
        horizon_mix[c["horizon"]] = horizon_mix.get(c["horizon"], 0) + 1
    return {
        "count": len(picks),
        "target_size": size,
        "expected_return_pct": round(exp_ret, 2),
        "avg_win_pct": avg_win,
        "aggregate_rr": round(sum(rr) / len(rr), 2) if rr else None,
        "total_alloc_pct": round(total_alloc, 1),
        "cash_pct": round(max(0.0, 100.0 - total_alloc), 1),
        "n_sectors": len({c.get("sector") for c in picks if c.get("sector")}),
        "n_independent": len(picks),          # one name per correlation cluster by construction
        "horizon_mix": horizon_mix,
        "carried_count": sum(1 for c in picks if c.get("carried")),
        "new_count": sum(1 for c in picks if c.get("is_new")),
    }


_DISCLAIMER = ("A curated best-of the full board, sized as one book. Expected-return figures are "
               "estimates from measured edge, not guarantees — decision support, not advice.")


def _empty(market: str, regime: Optional[dict], note: str) -> dict:
    return {
        "enabled": settings.basket_enabled, "market": market,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "size": _basket_size(regime), "regime_state": (regime or {}).get("state"),
        "items": [], "summary": _summary([], _basket_size(regime)),
        "sizing": {"enabled": settings.sizing_enabled, "sized": 0},
        "weights": basket_weights(), "params": basket_params(),
        "note": note,
    }


# Fields carried from a scored candidate onto a basket item (the deployable view).
_ITEM_KEYS = (
    "ticker", "horizon", "horizon_label", "price", "buy_low", "buy_high",
    "stop_pct", "stop_price", "target_pct", "target_price", "risk_reward",
    "conviction", "eligible_conviction", "avg_win_rate", "sector",
    "win_prob", "win_prob_source", "expected_return_pct", "basket_score", "basket_terms",
    "alloc_pct", "risk_pct", "kelly_used", "regime_fit", "regime_size_factor",
    "is_new", "carried", "zone", "live_price", "live_change_pct", "cluster", "cluster_peers",
    "side", "smart_money",
)


def _coattail_supportive(c: dict) -> bool:
    """True when the smart money is ACCUMULATING this name — the normalized smart_money signal
    runner attaches (13F/gurus, COT commercials, Hyperliquid top traders, NSE bulk buyers) points
    'accumulate'. The filter for the coattail basket."""
    sig = c.get("smart_money")
    return bool(sig) and sig.get("direction") == "accumulate"


def _coattail_distributing(c: dict) -> bool:
    """True when the smart money is DISTRIBUTING (selling / net-short) this name — the sell-side mirror
    of `_coattail_supportive`. The filter for the coattail SHORT sleeve (crypto whales net-short)."""
    sig = c.get("smart_money")
    return bool(sig) and sig.get("direction") == "distribute"


def build_basket(ideas: Optional[dict], clusters: Optional[dict] = None,
                 prev_basket: Optional[dict] = None, *, hold_mode: bool = False,
                 coattail_mode: bool = False, distribute_mode: bool = False) -> dict:
    """Distil an enriched horizon board into one curated, sized, sticky basket. Pure (no I/O).

    `ideas`       — the board from `runner.horizon_ideas` **after** `enrich_ideas_live` (so candidates
                    carry rank_score / factors / zone / calibration).
    `clusters`    — the board-wide symbol→cluster map from de-dup (cross-horizon); {} / None ⇒ no de-dup.
    `prev_basket` — the last committed basket (for hysteresis); None ⇒ a fresh basket.
    `hold_mode`   — build the COMMITTED hold book instead of the daily basket: a conviction floor, a
                    longer-horizon score tilt, wider hysteresis, and the hold-book size range → a smaller,
                    higher-conviction, stickier set built for staying power. False ⇒ the daily basket, unchanged.
    `coattail_mode` — build the COATTAIL basket: same daily sizing/scoring, but keep ONLY names the smart
                    money is accumulating (services/smart_money) — "what to buy that the pros are buying too".
    `distribute_mode` — build the sell-side COATTAIL SHORT sleeve: pool SHORT ideas and keep ONLY names the
                    smart money is DISTRIBUTING (crypto whales net-short) — "the short book the pros are on".
                    Regime size + risk haircut are inverted (a risk-off tide is a tailwind for shorts).

    Returns the basket dict (see `_empty` for the shape). Disabled or an empty board ⇒ an empty basket.
    """
    market = (ideas or {}).get("market", "IN") if ideas else "IN"
    regime = (ideas or {}).get("regime")
    if not settings.basket_enabled:
        return _empty(market, regime, "Curated basket disabled.")
    if not ideas or not ideas.get("buckets"):
        return _empty(market, regime, "No trade ideas yet — run a backtest for this market first.")

    pool = _pool(ideas, side="short" if distribute_mode else "long")
    if not pool:
        return _empty(market, regime,
                      "No current short signals to curate." if distribute_mode
                      else "No current buy signals to curate.")
    # A basket is "enter these now", so drop names whose live price has already SPENT the plan — broken
    # its stop or reached its target (a bad fresh entry). Offline (zone=None) nothing is dropped. The
    # full board below still shows them with their zone pill.
    spent = sum(1 for c in pool if c.get("zone") in _BROKEN_ZONES)
    pool = [c for c in pool if c.get("zone") not in _BROKEN_ZONES]
    # Hold mode: only well-agreed names are hold-worthy (conviction floor).
    if hold_mode:
        pool = [c for c in pool if (c.get("conviction") or 0) >= settings.hold_min_conviction]
    # Coattail mode: only names the smart money is accumulating. Distribute mode: only shorts the smart
    # money is distributing (the sell-side mirror).
    n_backed = 0
    if coattail_mode:
        pool = [c for c in pool if _coattail_supportive(c)]
        n_backed = len(pool)
    elif distribute_mode:
        pool = [c for c in pool if _coattail_distributing(c)]
        n_backed = len(pool)
    if not pool:
        note = ("No names meet the hold-book conviction floor right now." if hold_mode
                else "No current ideas have smart-money backing — nobody notable is accumulating these yet."
                if coattail_mode
                else "No shorts have smart-money backing — the pros aren't notably net-short these yet."
                if distribute_mode
                else "Every current idea has already hit its stop or target — nothing fresh to enter.")
        return _empty(market, regime, note)
    _attach_scores(pool)
    # Hold mode: tilt scores toward longer holds (staying power) — short-term setups churn fastest.
    if hold_mode:
        for c in pool:
            c["basket_score"] = round((c.get("basket_score") or 0.0)
                                      * HOLD_HORIZON_FACTOR.get(c.get("horizon"), 1.0), 4)

    size = _basket_size(regime, hold_mode, invert=distribute_mode)
    tilt = (regime or {}).get("tilt") or 0.0
    if distribute_mode:
        tilt = -tilt          # a short book's tailwind is the market's headwind (risk-off favours shorts)
    incumbents = _incumbent_keys(prev_basket, pool)
    margin = settings.hold_hysteresis_margin if hold_mode else settings.basket_hysteresis_margin

    def _effective(c: dict) -> float:
        s = c.get("basket_score") or 0.0
        return s * (1.0 + margin) if (c["ticker"], c["horizon"]) in incumbents else s

    picks = greedy_select(pool, clusters, size=size,
                          sector_cap=settings.basket_sector_cap, effective_score=_effective)
    for c in picks:
        c["carried"] = (c["ticker"], c["horizon"]) in incumbents

    sizing_summary = _size_basket(picks, tilt)
    items = [{k: c.get(k) for k in _ITEM_KEYS if k in c} for c in picks]
    summary = _summary(picks, size)
    summary["excluded_spent"] = spent      # ideas dropped because live price already hit stop/target
    if coattail_mode or distribute_mode:
        summary["smart_money_backed"] = n_backed   # names with smart-money backing to choose from
    return {
        "enabled": True, "market": market, "hold_mode": hold_mode, "coattail_mode": coattail_mode,
        "distribute_mode": distribute_mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "size": size, "regime_state": (regime or {}).get("state"),
        "items": items,
        "summary": summary,
        "sizing": sizing_summary,
        "weights": basket_weights(), "params": basket_params(),
        "note": _DISCLAIMER,
    }


# ── Hold-basket rebalance advisor — change only on a MATERIAL event ──
# The committed hold book stays put; this decides the (usually empty) set of changes to propose. The bar
# for a change is deliberately HIGH so a still-valid holding is never churned just for slipping a rank:
#   EXIT — the plan is spent (hit stop / hit target) or the thesis broke (no live signal, or conviction
#          fell below the hold floor).
#   ADD  — only to (a) refill a slot an EXIT just freed, or (b) a fresh name whose conviction is ≥ the
#          weakest holding's + a gap (a *dramatically* better idea) → one bounded swap.
#   HOLD — everything else. Pure — a function of numbers the caller assembles (live marks + board lookup).


def _ret_pct(entry, current) -> Optional[float]:
    if entry and current is not None and entry > 0:
        return round((current - entry) / entry * 100, 2)
    return None


def _hold_action(h: dict, action: str, reason: str, target_weight: float = 0.0) -> dict:
    ret = _ret_pct(h.get("entry_price"), h.get("current_price"))
    return {"ticker": h["ticker"], "action": action, "reason": reason,
            "held_weight": round(h.get("weight") or 0.0, 4), "target_weight": round(target_weight, 4),
            "entry_price": h.get("entry_price"), "current_price": h.get("current_price"),
            "return_pct": ret, "conviction": h.get("conviction")}


def _add_action(it: dict, reason: str) -> dict:
    return {"ticker": it["ticker"], "action": "ADD", "reason": reason,
            "held_weight": 0.0, "target_weight": round((it.get("alloc_pct") or 0.0) / 100.0, 4),
            "entry_price": None, "current_price": it.get("live_price") or it.get("price"),
            "return_pct": None, "conviction": it.get("conviction")}


def hold_rebalance_plan(held: list[dict], fresh_items: list[dict], *, target_size: int,
                        min_conviction: Optional[int] = None, conv_gap: Optional[int] = None) -> dict:
    """Compute the MATERIAL changes to move a committed hold book toward the fresh hold basket. Pure.

    `held`        — current holdings, each {ticker, weight, entry_price, current_price, zone, conviction}
                    (zone/conviction from today's live board; conviction None ⇒ the name is off the board).
    `fresh_items` — today's fresh hold-mode basket items (carry conviction, alloc_pct).
    Returns {actions, material, counts, target} — `target` is the resolved new holding tickers; `material`
    is True iff any EXIT/ADD is proposed (nothing to do otherwise)."""
    minc = settings.hold_min_conviction if min_conviction is None else min_conviction
    gap = settings.hold_displacement_conviction_gap if conv_gap is None else conv_gap
    held_tickers = {h["ticker"] for h in held}

    actions: list[dict] = []
    surviving: list[dict] = []
    for h in held:
        zone, conv = h.get("zone"), h.get("conviction")
        ret = _ret_pct(h.get("entry_price"), h.get("current_price"))
        rtxt = f"{'+' if (ret or 0) >= 0 else ''}{ret}%" if ret is not None else "—"
        if zone == "below_stop":
            actions.append(_hold_action(h, "EXIT", f"hit its stop ({rtxt})"))
        elif zone == "at_target":
            actions.append(_hold_action(h, "EXIT", f"reached its target ({rtxt})"))
        elif conv is None:
            actions.append(_hold_action(h, "EXIT", "thesis broke — no live signal on the board"))
        elif conv < minc:
            actions.append(_hold_action(h, "EXIT", f"conviction weakened to {conv} (below {minc})"))
        else:
            actions.append(_hold_action(h, "HOLD", "still valid — staying put", target_weight=h.get("weight") or 0.0))
            surviving.append(h)

    # Fresh names not held, strongest first (conviction then score).
    avail = sorted((it for it in fresh_items if it["ticker"] not in held_tickers),
                   key=lambda it: (it.get("conviction") or 0, it.get("basket_score") or 0.0), reverse=True)
    adds: list[dict] = []
    # (a) refill slots freed by exits (or an under-filled book), best-first.
    while len(surviving) + len(adds) < target_size and avail:
        it = avail.pop(0)
        adds.append(it)
        actions.append(_add_action(it, "new high-conviction pick"))
    # (b) one bounded, high-bar displacement — a dramatically higher-conviction fresh name bumps the
    #     weakest holding. Nothing here unless the gap is real, so a good holding is never churned.
    if surviving and avail:
        best = avail[0]
        weakest = min(surviving, key=lambda h: (h.get("conviction") or 0))
        if (best.get("conviction") or 0) >= (weakest.get("conviction") or 0) + gap:
            actions = [a for a in actions if a["ticker"] != weakest["ticker"]]
            actions.append(_hold_action(weakest, "EXIT",
                                        f"displaced by higher-conviction {best['ticker']}"))
            surviving.remove(weakest)
            adds.append(best)
            actions.append(_add_action(best, f"much higher conviction than {weakest['ticker']}"))

    material = any(a["action"] in ("EXIT", "ADD", "BUY") for a in actions)
    _order = {"EXIT": 0, "ADD": 1, "BUY": 1, "HOLD": 2}
    actions.sort(key=lambda a: _order.get(a["action"], 9))
    counts: dict[str, int] = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    target = [h["ticker"] for h in surviving] + [it["ticker"] for it in adds]
    return {"actions": actions, "material": material, "counts": counts, "target": target}


# ── Best-Odds board — the day's highest-win-probability trades ───────
# The simplest possible answer to "what's most likely to WIN": rank the whole board's fresh setups
# PURELY by win probability and take the top few. No portfolio construction (no ¼-Kelly sizing, no
# regime scaling, no stickiness) — that's `build_basket`'s job (the diversified book to hold). This is
# the "pick-one-and-go" leaderboard. It shares the basket's per-idea win-prob numbers (via
# `_attach_scores` → `sizing._win_prob`: learned-fusion → calibrated → advertised), so the two views
# never disagree on an idea's odds.

_BEST_ODDS_DISCLAIMER = ("Ranked purely by each setup's best-available win rate (learned/calibrated "
                         "where we have the history, else the backtest's advertised rate — see the ✓). "
                         "A high hit-rate is not a high return; check the plan and reward:risk. "
                         "Decision-support, not advice.")

# Fields carried onto a Best-Odds item — the win-probability view (no portfolio-sizing fields).
_BEST_ODDS_KEYS = tuple(k for k in _ITEM_KEYS if k not in ("alloc_pct", "risk_pct", "kelly_used")) + (
    "pillar_count", "tier", "pillars", "typical_drawdown_pct", "deep_drawdown_pct",
)


def best_odds_params() -> dict:
    """The active Best-Odds board configuration — attached to the payload so the UI can explain it."""
    return {
        "enabled": settings.best_odds_enabled,
        "size": settings.best_odds_size,
        "cluster_dedup": settings.best_odds_cluster_dedup,
    }


def _best_odds_summary(picks: list[dict]) -> dict:
    """Board roll-up: count, mean win prob, aggregate reward:risk, and a win-prob SOURCE tally so the UI
    can disclose how many odds are model/calibrated-backed vs raw advertised backtest rates."""
    wins = [c["win_prob"] for c in picks if c.get("win_prob") is not None]
    rr = [c["risk_reward"] for c in picks if c.get("risk_reward") is not None]
    sources: dict[str, int] = {}
    for c in picks:
        src = c.get("win_prob_source")
        if src:
            sources[src] = sources.get(src, 0) + 1
    return {
        "count": len(picks),
        "avg_win_pct": round(sum(wins) / len(wins), 1) if wins else None,
        "avg_reward_risk": round(sum(rr) / len(rr), 2) if rr else None,
        "win_prob_sources": sources,        # e.g. {"learned": 2, "calibrated": 1, "advertised": 2}
        "backed_count": sum(n for s, n in sources.items() if s in ("learned", "calibrated")),
    }


def _empty_best_odds(market: str, regime: Optional[dict], size: int, note: str) -> dict:
    return {
        "enabled": settings.best_odds_enabled, "market": market,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "size": size, "regime_state": (regime or {}).get("state"),
        "items": [], "summary": _best_odds_summary([]), "params": best_odds_params(),
        "note": note,
    }


def build_best_odds(ideas: Optional[dict], clusters: Optional[dict] = None, *,
                    top_n: Optional[int] = None) -> dict:
    """Rank the enriched board's fresh setups PURELY by win probability and surface the top few. Pure
    (no I/O). Same win-prob/EV numbers as `build_basket` (via `_attach_scores`), so odds never disagree.

    `ideas`    — the board from `runner.horizon_ideas` **after** `enrich_ideas_live`.
    `clusters` — the board-wide symbol→cluster map (cross-horizon de-dup), so the picks are DISTINCT
                 bets rather than variants of one name. None ⇒ ticker-level de-dup only.
    `top_n`    — how many to surface (default `settings.best_odds_size`).

    Returns the board dict (see `_empty_best_odds`); disabled / empty ⇒ an empty board.
    """
    market = (ideas or {}).get("market", "IN") if ideas else "IN"
    regime = (ideas or {}).get("regime")
    n = int(top_n if top_n is not None else settings.best_odds_size)
    if not settings.best_odds_enabled:
        return _empty_best_odds(market, regime, n, "Best-Odds board disabled.")
    if not ideas or not ideas.get("buckets"):
        return _empty_best_odds(market, regime, n,
                                "No trade ideas yet — run a backtest for this market first.")

    pool = _pool(ideas)
    # Only fresh entries: drop names whose live price already SPENT the plan (stopped / hit target) —
    # a bad fresh entry. Offline (zone=None) nothing is dropped; the full board still shows them.
    pool = [c for c in pool if c.get("zone") not in _BROKEN_ZONES]
    if not pool:
        return _empty_best_odds(market, regime, n,
                                "Every current idea has already hit its stop or target — nothing fresh to enter.")
    _attach_scores(pool)      # sets win_prob (%) / win_prob_source / expected_return_pct

    # Rank PURELY by win probability (desc). Deterministic tie-breaks: reward:risk, then EV. A missing
    # win prob sinks to the bottom (an idea we can't estimate shouldn't top a "most likely to win" board).
    pool.sort(key=lambda c: (
        c.get("win_prob") if c.get("win_prob") is not None else -1.0,
        c.get("risk_reward") or 0.0,
        c.get("expected_return_pct") or 0.0,
    ), reverse=True)

    # De-dup to DISTINCT names: one row per ticker (a name recurs across horizons — its highest-win-prob
    # copy is first after the sort) and one per correlation cluster when enabled. De-dup is about
    # distinctness, NOT ranking — the order above is untouched.
    picks: list[dict] = []
    seen_tickers: set = set()
    seen_clusters: set = set()
    for c in pool:
        tk = c["ticker"]
        if tk in seen_tickers:
            continue
        cid = clusters.get(tk) if clusters else None
        if settings.best_odds_cluster_dedup and cid is not None and cid in seen_clusters:
            continue
        picks.append(c)
        seen_tickers.add(tk)
        if cid is not None:
            seen_clusters.add(cid)
        if len(picks) >= n:
            break

    items = []
    for i, c in enumerate(picks, 1):
        it = {k: c.get(k) for k in _BEST_ODDS_KEYS if k in c}
        it["rank"] = i
        items.append(it)
    return {
        "enabled": True, "market": market,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "size": n, "regime_state": (regime or {}).get("state"),
        "items": items,
        "summary": _best_odds_summary(picks),
        "params": best_odds_params(),
        "note": _BEST_ODDS_DISCLAIMER,
    }
