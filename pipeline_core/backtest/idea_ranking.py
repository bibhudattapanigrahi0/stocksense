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

FACTOR_NAMES = ("earnings", "news", "sector", "quality", "smart_money")

# Smart-money signal conviction → factor magnitude (direction sets the sign).
SMART_MONEY_CONVICTION = {"high": 1.0, "medium": 0.6, "low": 0.3}


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


def smart_money_factor(sig: Optional[dict]) -> Optional[float]:
    """Smart-money accumulation as a symmetric tilt (long-only board): the pros ACCUMULATING lifts
    an idea, DISTRIBUTING trims it, scaled by the signal's conviction. Market-agnostic over the
    normalized smart-money signal (services/smart_money): 13F QoQ (US), COT (metals), Hyperliquid
    whale net-flow (crypto), bulk deals (IN). None → neutral (no signal / can't judge direction).

    Prefers the signal's own `net_delta_pct` magnitude when present (a −100..+100 read → [−1, +1]),
    else falls back to the discrete conviction bucket. Sign always comes from the direction."""
    if not sig or not sig.get("available"):
        return None
    direction = sig.get("direction")
    if direction not in ("accumulate", "distribute"):
        return None
    sign = 1.0 if direction == "accumulate" else -1.0
    delta = sig.get("net_delta_pct", sig.get("qoq_change_pct"))
    if delta is not None:
        mag = min(abs(float(delta)) / 100.0, 1.0)               # |Δ| 100% → full strength
    else:
        mag = SMART_MONEY_CONVICTION.get(sig.get("conviction"), 0.3)
    return round(_clamp(sign * max(mag, 0.15)), 4)              # floor so a confirmed signal always counts


# Family → the short chip shown when the smart money is DISTRIBUTING (selling / net-short) a name.
_DISTRIBUTE_LABEL = {
    "ownership": "🏦 13F distributing", "congress": "🏛 Congress selling",
    "cot": "🏦 COT net-short", "flow": "🐋 whales net-short", "bulk_deal": "🇮🇳 bulk sellers",
    "ace": "🇮🇳 ace exiting",
}


def distribution_flag(sig: Optional[dict], side: str = "long") -> Optional[dict]:
    """A small, visible sell-side flag when the smart money is DISTRIBUTING this name — else None. On a
    LONG it reads as an AVOID warning ("the pros are trimming"); on a crypto SHORT it reads as a
    CONFIRMATION ("their selling is a tailwind"). Pure — reads only the normalized smart_money signal.

    The RANK impact is already applied elsewhere (smart_money_factor is negative for distribution, and
    _rank_pool inverts it for a short); this only SURFACES what is otherwise a silent multiplier."""
    if not sig or not sig.get("available") or sig.get("direction") != "distribute":
        return None
    fam = sig.get("family")
    return {
        "label": _DISTRIBUTE_LABEL.get(fam, "🚩 smart money distributing"),
        "detail": sig.get("detail"),
        "family": fam,
        "stance": "confirm" if side == "short" else "avoid",
        "actors": sig.get("actors") or [],
        "n_actors": sig.get("n_actors"),
    }


def smart_money_exit_flag(sig: Optional[dict], side: str = "long") -> Optional[dict]:
    """A held-position EXIT nudge when the smart money has turned AGAINST an open trade — the pros now
    DISTRIBUTING a long you hold (or ACCUMULATING a short you hold). None otherwise. Read-only — this
    never auto-closes a position; it's a "consider trimming/covering" signal for the tracker. Pure."""
    if not sig or not sig.get("available"):
        return None
    direction = sig.get("direction")
    against = "accumulate" if side == "short" else "distribute"
    if direction != against:
        return None
    fam = sig.get("family")
    if side == "short":
        label, msg = "🚩 smart money accumulating", "Smart money is now accumulating this — consider covering the short."
    else:
        label, msg = _DISTRIBUTE_LABEL.get(fam, "🚩 smart money exiting"), "Smart money is now distributing this — consider trimming."
    return {"label": label, "detail": sig.get("detail"), "family": fam,
            "message": msg, "actors": sig.get("actors") or [], "n_actors": sig.get("n_actors")}


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
        "smart_money": settings.rank_w_smart_money,
        "min_multiplier": settings.rank_min_multiplier,     # quality gate: drop below this
        "earnings_window_days": EARNINGS_WINDOW_DAYS, "sector_rs_scale": SECTOR_RS_SCALE,
    }


def composite_multiplier(factors: dict, weights: Optional[dict] = None) -> float:
    """Π (1 + wᵢ · factorᵢ) over the factor values present in `factors` (keys in FACTOR_NAMES).
    A missing/None factor or a zero weight contributes ×1, so the result is 1.0 when nothing
    applies — leaving the base ranking untouched."""
    w = weights or {"earnings": settings.rank_w_earnings, "news": settings.rank_w_news,
                    "sector": settings.rank_w_sector, "quality": settings.rank_w_quality,
                    "smart_money": settings.rank_w_smart_money}
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


def idea_pillars(c: dict, side: str = "long") -> dict:
    """Which independent families support this idea: True (supports), False (measured, doesn't), or None
    (not available this market). Pure function of numbers the candidate already carries post-enrichment.

    For a SHORT (`side="short"`) the DIRECTIONAL families flip — high quality, bullish news, a leading
    sector and smart-money accumulation all argue AGAINST a short, so a supportive reading becomes
    non-supportive. `regime_fit` is already computed from the short's perspective by the caller, so
    the regime pillar is read as-is; `technical` (a strategy fired) supports either direction."""
    qs = c.get("quality_score")
    ns = c.get("news_score")
    rs = c.get("sector_rs")
    rf = c.get("regime_fit")
    short = side == "short"

    def _dir(supportive: Optional[bool]) -> Optional[bool]:
        return (not supportive) if (short and supportive is not None) else supportive

    return {
        "technical": (c.get("eligible_conviction", c.get("conviction", 0)) or 0) >= 1,
        "quality": _dir((qs >= QUALITY_PILLAR_MIN) if qs is not None else None),
        "smart_money": _dir(_smart_money_supportive(c.get("smart_money") or c.get("inst_ownership"))),
        "regime": (rf == "tailwind") if rf is not None else None,
        "sentiment": _dir((ns >= NEWS_PILLAR_MIN) if ns is not None else None),
        "sector": _dir((rs > 0) if rs is not None else None),
    }


def pillar_summary(c: dict, available: Optional[int] = None, side: str = "long") -> dict:
    """`idea_pillars` plus the count of supporting families and how many were available to confirm.
    `available` (families switched on for this market, from the caller) defaults to the number of
    non-None readings on this idea when not supplied (standalone/testing use). `side` flips the
    directional pillars for shorts (see `idea_pillars`)."""
    p = idea_pillars(c, side)
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


def pillar_disclosure(market: Optional[str] = None) -> dict:
    """The confluence config, surfaced in the ideas payload so the UI can explain the Actionable/Watchlist
    split (mirrors `rank_weights` for the composite banner). Crypto uses a lower actionable bar (it has
    only ~3 possible families), so the disclosed bar is market-specific to match what tiering applies."""
    min_actionable = (settings.pillar_min_actionable_crypto
                      if (market or "").upper() == "CRYPTO" else settings.pillar_min_actionable)
    return {
        "enabled": settings.pillar_tiering_enabled,
        "min_actionable": min_actionable,
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


# ── Rank efficiency (the north-star: does the ranking predict realized return?) ──
# Conviction / factors / calibration all try to ORDER the board well. This measures whether that
# ordering actually works: the Spearman rank-IC between an idea's entry rank and its realized return,
# plus the mean realized return of the best-ranked quintile vs the worst. Every other ranking change
# (weights, meta-labeling, gates) should be adopted only if it moves these numbers — measure, then tune.

def _avg_ranks(vals: list) -> list:
    """1-based ranks with ties averaged (the rank transform behind Spearman)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0                 # mean 1-based position of the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list, ys: list) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else 0.0


def _spearman(xs: list, ys: list) -> float:
    """Spearman ρ = Pearson on the (tie-averaged) ranks. No numpy/scipy dependency."""
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def rank_efficiency(closed: list, *, min_samples: Optional[int] = None) -> dict:
    """Does entry rank predict realized return? Over CLOSED tracked ideas, the Spearman IC between the
    entry-time within-cohort rank (`entry_rank_pct`, 1=best) and `realized_return_pct`, plus the mean
    realized return of the top vs bottom rank quintile and their spread. `active=False` (with n) until
    enough closed ideas carry an `entry_rank_pct` (older records predate it and are simply skipped).

    This is the board's report card: IC > 0 and a positive top−bottom spread mean the ranking earns its
    keep. Pure — reads only the ledger."""
    thr = settings.rank_ic_min_samples if min_samples is None else min_samples
    rows = [(c.get("entry_rank_pct"), c.get("realized_return_pct")) for c in closed
            if c.get("entry_rank_pct") is not None and c.get("realized_return_pct") is not None]
    n = len(rows)
    if n < thr:
        return {"active": False, "n": n, "min_samples": thr}
    xs = [r for r, _ in rows]
    ys = [ret for _, ret in rows]
    order = sorted(rows, key=lambda t: t[0])          # ascending rank pct (worst → best)
    k = max(1, n // 5)
    bottom = [ret for _, ret in order[:k]]
    top = [ret for _, ret in order[-k:]]
    top_mean, bot_mean = sum(top) / len(top), sum(bottom) / len(bottom)
    return {"active": True, "n": n, "min_samples": thr,
            "ic": round(_spearman(xs, ys), 3),
            "top_quintile_return_pct": round(top_mean, 2),
            "bottom_quintile_return_pct": round(bot_mean, 2),
            "spread_pct": round(top_mean - bot_mean, 2)}
