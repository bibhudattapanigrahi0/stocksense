"""
Multi-Bagger screen — a **fundamentals-first** long-horizon idea generator.

The daily Trade Ideas board (`backtest/runner.horizon_ideas`) is *technical*: it answers "what's a
good entry right now?". This module answers a different question — "which businesses could compound
many times over?" — the Peter Lynch "ten-bagger" idea: small, fast-growing, high-quality,
well-owned companies whose growth isn't fully priced in yet.

A company gets a **0–100 multi-bagger score** = the weighted mean of six fundamental pillars, each
normalised to 0..1 from whatever metrics are actually present (missing metrics are skipped and the
pillar re-normalises, so a name is never flattered by absent data):

    growth · quality · balance-sheet · valuation-sanity · size/runway · ownership

On top of that base sits a small, **bounded** technical overlay (`multibagger_w_technical`): a name
still early in its 52-week range — or one a strategy is signalling a buy on today — gets a mild
nudge, but the overlay is capped so it can never reorder the fundamental leaders. It's *timing
context*, not a gate.

Screening is **staged cheap→expensive** (like `agents/screener`): the whole universe is scored from
the 1-day-cached info stage (`market_data.get_stock_info`); only the top survivors pay for the deep
stage (financial statements → Piotroski / ROCE / 3y CAGRs, plus IN promoter holding). The ranked
result is persisted per market so the tab loads instantly, with an on-demand background refresh.

The pillar/overlay math is pure and network-free (unit-tested in `tests/test_multibagger.py`); only
`screen_multibaggers()` touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from pipeline_core.backtest import idea_ranking
from pipeline_core.config import DATA_DIR, settings
from pipeline_core.markets import get_market, get_universe_tickers
from pipeline_core.services import market_data, nse_data
from pipeline_core.utils import round_price

logger = logging.getLogger("stocksense.multibagger")

RESULTS_DIR = DATA_DIR / "backtests"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# The six fundamental pillars, in display order. Weights live in settings.multibagger_w_* so they're
# tunable; the technical overlay (settings.multibagger_w_technical) is a separate bounded multiplier.
PILLARS = ("growth", "quality", "balance", "value", "size", "ownership")

# Market-cap → "size/runway" anchors, per currency scale: (small_log10, mega_log10). A name at/below
# the small anchor scores 1 (max runway); at/above the mega anchor scores 0 (little room to 10x).
# IN caps are in INR (₹1,000 Cr ≈ 1e10 → 1e13 for a ₹10-lakh-Cr mega); US caps are in USD.
_SIZE_ANCHORS = {"IN": (10.0, 13.0), "US": (9.0, 12.0)}
_SIZE_ANCHOR_DEFAULT = (9.0, 12.0)

DISCLAIMER = (
    "Multi-bagger candidates are a fundamentals-first research screen — high-growth, high-quality, "
    "smaller-cap businesses whose growth may not be fully priced in. Smaller companies are more "
    "volatile and can fall as fast as they rise; a high score is a starting point for your own due "
    "diligence, not a prediction or investment advice. Fundamentals are point-in-time (yfinance / "
    "NSE, ~quarterly) and can be stale or incomplete."
)


# ── Pure sub-score helpers (each returns 0..1, or None when its data is absent) ──

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp_up(x: Optional[float], lo: float, hi: float) -> Optional[float]:
    """x mapped from [lo, hi] → [0, 1] (higher is better). None passes through."""
    if x is None:
        return None
    return _clamp01((float(x) - lo) / (hi - lo))


def _ramp_down(x: Optional[float], good: float, bad: float) -> Optional[float]:
    """x mapped so `good` → 1 and `bad` → 0 (lower is better). None passes through."""
    if x is None:
        return None
    return _clamp01((bad - float(x)) / (bad - good))


def _mean(vals: list[Optional[float]]) -> Optional[float]:
    present = [v for v in vals if v is not None]
    return sum(present) / len(present) if present else None


def _debt_to_equity_x(de: Optional[float]) -> Optional[float]:
    """yfinance reports debt/equity as a percent (50 = 0.5×); normalise to a ratio."""
    if de is None:
        return None
    de = float(de)
    return de / 100.0 if de > 5 else de


# ── Pillars (data = a get_stock_info dict, optionally augmented with deep-stage keys) ──

def growth_score(data: dict) -> Optional[float]:
    """Top- and bottom-line growth. Info: revenue/earnings growth (fractions). Deep: 3y sales/EPS CAGR."""
    return _mean([
        _ramp_up(data.get("revenue_growth"), 0.0, 0.30),      # 0% → 0, ≥30% → 1
        _ramp_up(data.get("earnings_growth"), 0.0, 0.35),
        _ramp_up(data.get("sales_growth_3y"), 0.0, 0.30),     # deep
        _ramp_up(data.get("eps_growth_3y"), 0.0, 0.35),       # deep
    ])


def quality_pillar(data: dict) -> Optional[float]:
    """Profitability & capital efficiency. Reuses idea_ranking.quality_score (ROE/debt/margins),
    blended with the deep-stage Piotroski F-score and ROCE when available."""
    q = idea_ranking.quality_score(data)          # 0..100 (50 neutral), or None
    return _mean([
        (q / 100.0) if q is not None else None,
        _ramp_up(data.get("piotroski_score"), 3.0, 8.0),      # deep: 3/9 → 0, ≥8/9 → 1
        _ramp_up(data.get("roce"), 0.08, 0.30),               # deep: 8% → 0, ≥30% → 1
    ])


def balance_score(data: dict) -> Optional[float]:
    """Solvency & self-funding: low leverage, positive free cash flow, comfortable interest cover."""
    fcf = data.get("free_cash_flow")
    fcf_sub = None if fcf is None else (1.0 if float(fcf) > 0 else 0.0)
    return _mean([
        _ramp_down(_debt_to_equity_x(data.get("debt_to_equity")), 0.1, 2.0),   # <0.1× → 1, ≥2× → 0
        fcf_sub,
        _ramp_up(data.get("interest_coverage"), 1.0, 8.0),    # deep: 1× → 0, ≥8× → 1
    ])


def value_score(data: dict) -> Optional[float]:
    """Valuation *sanity* — is the growth already fully priced? PEG-first (growth-adjusted); falls
    back to a plain P/E band. Loss-makers (non-positive PEG/PE) get a low, not absent, read."""
    peg = data.get("peg_ratio")
    if peg is not None:
        peg = float(peg)
        return 0.2 if peg <= 0 else _clamp01((2.5 - peg) / 2.0)   # ≤0.5 → 1, ≥2.5 → 0
    pe = data.get("pe_ratio")
    if pe is not None:
        pe = float(pe)
        return 0.2 if pe <= 0 else _clamp01((45.0 - pe) / 40.0)   # ≤5 → 1, ≥45 → 0
    return None


def size_score(data: dict, market: str = "IN") -> Optional[float]:
    """Runway: smaller companies have more room to compound. Log-scaled between per-market anchors."""
    mcap = data.get("market_cap")
    if not mcap or float(mcap) <= 0:
        return None
    small_log, mega_log = _SIZE_ANCHORS.get((market or "IN").upper(), _SIZE_ANCHOR_DEFAULT)
    return _clamp01((mega_log - math.log10(float(mcap))) / (mega_log - small_log))


def ownership_score(data: dict) -> Optional[float]:
    """Skin in the game: high insider ownership, and (deep, IN) a high, unpledged promoter stake."""
    insiders = data.get("held_pct_insiders")     # fraction, e.g. 0.30
    prom = data.get("promoter_holding_pct")       # deep IN, 0..100
    pledge = data.get("pledge_pct")               # deep IN, 0..100 (lower better)
    return _mean([
        _ramp_up(insiders, 0.0, 0.30),
        _ramp_up(prom, 30.0, 65.0),
        _ramp_down(pledge, 0.0, 30.0),
    ])


def pillar_scores(data: dict, market: str = "IN") -> dict[str, Optional[float]]:
    """All six pillar sub-scores (0..1 or None) for a merged info/deep dict."""
    return {
        "growth": growth_score(data),
        "quality": quality_pillar(data),
        "balance": balance_score(data),
        "value": value_score(data),
        "size": size_score(data, market),
        "ownership": ownership_score(data),
    }


def _pillar_weights() -> dict[str, float]:
    return {
        "growth": settings.multibagger_w_growth,
        "quality": settings.multibagger_w_quality,
        "balance": settings.multibagger_w_balance,
        "value": settings.multibagger_w_value,
        "size": settings.multibagger_w_size,
        "ownership": settings.multibagger_w_ownership,
    }


def _52w_position(data: dict) -> Optional[float]:
    """Where the price sits in its 52-week range: 0 = at the low, 1 = at the high. None if unknown."""
    price = data.get("price")
    lo, hi = data.get("week_52_low"), data.get("week_52_high")
    if price is None or lo is None or hi is None or hi <= lo:
        return None
    return _clamp01((float(price) - float(lo)) / (float(hi) - float(lo)))


def technical_overlay(data: dict, buy_signal: bool) -> float:
    """A bounded [-1, 1] timing nudge: leans positive for a name still early in its 52-week range or
    with a live strategy buy, negative for an extended one. Deliberately mild — applied as a small
    multiplier so it never overrides the fundamental ranking."""
    pos = _52w_position(data)
    parts = []
    if pos is not None:
        parts.append(_clamp(-1.0, 1.0, (0.6 - pos) / 0.6))   # early (<60% of range) lifts, extended trims
    if buy_signal:
        parts.append(0.5)
    if not parts:
        return 0.0
    return _clamp(-1.0, 1.0, sum(parts) / len(parts))


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


# ── Reasons / concerns (human-readable "why it could multi-bag") ──

_REASON_TEXT = {
    "growth": "Fast, sustained revenue & earnings growth",
    "quality": "High-quality, profitable compounder (strong returns on capital)",
    "balance": "Strong balance sheet — low debt and cash-generative",
    "value": "Reasonably priced for its growth rate",
    "size": "Small enough to have real room to compound",
    "ownership": "High insider / promoter ownership — aligned skin in the game",
}
_CONCERN_TEXT = {
    "growth": "Slow or uneven growth",
    "quality": "Weak profitability / returns on capital",
    "balance": "Leveraged or burning cash",
    "value": "Richly valued relative to its growth",
    "size": "Large-cap — limited multi-bagger runway",
    "ownership": "Low insider ownership or a pledged promoter stake",
}


def _reasons_and_concerns(pillars: dict[str, Optional[float]], buy_signal: bool) -> tuple[list, list]:
    reasons, concerns = [], []
    for name, sub in pillars.items():
        if sub is None:
            continue
        if sub >= 0.66:
            reasons.append((sub, _REASON_TEXT[name]))
        elif sub <= 0.34:
            concerns.append((1 - sub, _CONCERN_TEXT[name]))
    reasons.sort(reverse=True)
    concerns.sort(reverse=True)
    out_r = [t for _, t in reasons[:4]]
    if buy_signal:
        out_r.insert(0, "A strategy is signalling a buy now (timing tailwind)")
    return out_r, [t for _, t in concerns[:3]]


# ── Candidate scoring (pure) ──

def score_candidate(info: dict, market: str = "IN", *, buy_signal: bool = False) -> Optional[dict]:
    """Score one stock's `get_stock_info` dict (optionally augmented with deep-stage keys) into a
    multi-bagger candidate, or None if too little data is present (coverage gate) or it isn't scorable.

    The returned `score` (0..100) is the coverage-renormalised weighted pillar mean × a small, bounded
    technical multiplier. `pillars` are 0..100 (or None) for display; `reasons`/`concerns` explain it.
    """
    ticker = info.get("ticker") or info.get("symbol")
    if not ticker:
        return None
    subs = pillar_scores(info, market)
    present = {k: v for k, v in subs.items() if v is not None}
    if len(present) / len(PILLARS) < settings.multibagger_min_coverage:
        return None                      # not enough fundamentals to judge — drop, don't flatter

    weights = _pillar_weights()
    wsum = sum(weights[k] for k in present)
    if wsum <= 0:
        return None
    base = sum(weights[k] * v for k, v in present.items()) / wsum      # 0..1

    overlay = technical_overlay(info, buy_signal)
    score = _clamp01(base * (1 + settings.multibagger_w_technical * overlay)) * 100.0

    reasons, concerns = _reasons_and_concerns(subs, buy_signal)
    pos = _52w_position(info)
    return {
        "ticker": ticker,          # already display-formatted by get_stock_info
        "name": info.get("name") or ticker,
        "sector": info.get("sector"),
        "price": info.get("price"),
        "market_cap": info.get("market_cap"),
        "market_cap_display": info.get("market_cap_cr"),   # market-scaled (Cr / B), UI labels the unit
        "score": round(score, 1),
        "fundamental_score": round(base * 100.0, 1),
        "coverage": round(len(present) / len(PILLARS), 2),
        "pillars": {k: (round(v * 100.0, 1) if v is not None else None) for k, v in subs.items()},
        "reasons": reasons,
        "concerns": concerns,
        "buy_signal_now": bool(buy_signal),
        "pct_in_52w_range": round(pos * 100.0, 1) if pos is not None else None,
        "deep": bool(info.get("_deep")),
        "metrics": {
            "revenue_growth": info.get("revenue_growth"),
            "earnings_growth": info.get("earnings_growth"),
            "roe": info.get("roe"),
            "roce": info.get("roce"),
            "profit_margins": info.get("profit_margins"),
            "debt_to_equity": info.get("debt_to_equity"),
            "peg_ratio": info.get("peg_ratio"),
            "pe_ratio": info.get("pe_ratio"),
            "piotroski_score": info.get("piotroski_score"),
            "sales_growth_3y": info.get("sales_growth_3y"),
            "eps_growth_3y": info.get("eps_growth_3y"),
            "promoter_holding_pct": info.get("promoter_holding_pct"),
        },
    }


# ── Persistence (mirrors backtest/runner._persist / load_latest) ──

def _latest_file(market: str = "IN"):
    return RESULTS_DIR / f"multibaggers_{(market or 'IN').upper()}.json"


def _persist(payload: dict, market: str = "IN") -> None:
    dest = _latest_file(market)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(dest)
    logger.info(f"Multi-bagger screen persisted to {dest}")


def load_latest_multibaggers(market: str = "IN") -> Optional[dict]:
    """Last persisted multi-bagger screen for a market, or None if it hasn't been run yet."""
    path = _latest_file(market)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Could not read multi-bagger results ({path.name}): {e}")
        return None


# ── Background-refresh job state (mirrors runner.signals_job_state) ──

job_state: dict = {
    "running": False, "market": None, "stage": None, "progress_pct": 0.0,
    "detail": "", "started_at": None, "finished_at": None, "error": None,
}


def _set_job(stage: str, pct: float, detail: str = "") -> None:
    job_state.update(stage=stage, progress_pct=round(pct, 1), detail=detail)


# ── Universe + technical overlay + deep stage ──

def _universe_for(market: str) -> list[str]:
    """The broad mid/small-cap hunting ground for a market (that's where multi-baggers form), capped
    at settings.multibagger_max_universe. Falls back to the market's default universe on any error."""
    m = get_market(market)
    try:
        if m.key == "US":
            tickers = get_universe_tickers("US", "S&P 500") + get_universe_tickers("US", "NASDAQ-100")
        elif m.key == "IN":
            tickers = get_universe_tickers("IN", "NIFTY 500")
        else:
            tickers = get_universe_tickers(market, m.default_universe)
    except Exception as e:
        logger.warning("Multi-bagger universe fetch failed for %s (%s); using default universe", market, e)
        try:
            tickers = get_universe_tickers(market, m.default_universe)
        except Exception:
            tickers = []
    seen: dict[str, None] = {}
    for t in tickers:                      # de-dup, preserve order (US union overlaps)
        seen.setdefault(t, None)
    return list(seen)[: settings.multibagger_max_universe]


def _current_buy_set(market: str) -> set[str]:
    """Tickers a strategy is currently signalling a BUY on, from the latest backtest (technical
    overlay). Empty when there's no run — the overlay simply contributes nothing then."""
    try:
        from pipeline_core.backtest import runner
        latest = runner.load_latest(market)
    except Exception:
        return set()
    if not latest:
        return set()
    buys: set[str] = set()
    for s in latest.get("strategies", []):
        for b in (s.get("signals", {}) or {}).get("buy", []):
            if b.get("ticker"):
                buys.add(b["ticker"])
    return buys


async def _fetch_deep(ticker: str, market: str) -> dict:
    """Deep-stage metrics for one survivor: statement-derived quality (Piotroski / ROCE / 3y CAGRs /
    interest coverage) plus (IN) promoter holding & pledge. Best-effort — returns {} on failure."""
    from pipeline_core.agents.fundamental import compute_quality_metrics

    out: dict = {}
    try:
        fin = await market_data.aget_financials(ticker, market)
        if fin:
            q = compute_quality_metrics(fin)
            for k in ("roce", "interest_coverage", "piotroski_score", "eps_growth_3y", "sales_growth_3y"):
                if q.get(k) is not None:
                    out[k] = q[k]
    except Exception as e:
        logger.debug("Deep quality fetch failed for %s: %s", ticker, e)
    if (market or "IN").upper() == "IN":
        try:
            ph = await asyncio.to_thread(nse_data.get_promoter_holding, ticker)
            if ph:
                for k in ("promoter_holding_pct", "pledge_pct"):
                    if ph.get(k) is not None:
                        out[k] = ph[k]
        except Exception as e:
            logger.debug("Promoter-holding fetch failed for %s: %s", ticker, e)
    return out


def _empty_payload(market: str, note: str) -> dict:
    return {
        "market": (market or "IN").upper(), "candidates": [], "count": 0,
        "run_at": None, "universe": None, "weights": _pillar_weights(),
        "disclaimer": DISCLAIMER, "note": note,
    }


async def screen_multibaggers(market: str = "IN", top_n: Optional[int] = None,
                              *, persist: bool = True) -> dict:
    """Run the full staged multi-bagger screen for a market and return the ranked payload.

    info stage (whole universe, 1-day cached) → coverage gate + rank → deep stage on the top survivors
    → bounded technical overlay → persist + return. Gated to equity markets (`has_fundamentals`);
    price-only markets return an empty, note-bearing payload.
    """
    m = get_market(market)
    top_n = int(top_n or settings.multibagger_top_n)
    if not m.has_fundamentals:
        return _empty_payload(market, "Multi-bagger screening needs company fundamentals — available "
                                      "for equity markets (India / US) only.")

    started = datetime.now(timezone.utc)
    job_state.update(running=True, market=m.key, error=None, finished_at=None,
                     started_at=started.isoformat())
    _set_job("universe", 2.0, "Resolving universe")
    try:
        universe = _universe_for(market)
        if not universe:
            raise RuntimeError("empty universe")
        buy_set = _current_buy_set(market)

        # ── Info stage: score the whole universe from cached profiles ──
        total = len(universe)

        def _cb(done: int, _total: int) -> None:
            _set_job("fundamentals", 2.0 + 68.0 * (done / max(1, total)),
                     f"Fetching fundamentals {done}/{total}")

        infos = await market_data.fetch_batch_info(universe, progress_callback=_cb, market=market)
        _set_job("scoring", 74.0, f"Scoring {len(infos)} companies")
        scored = []
        for info in infos:
            cand = score_candidate(info, market, buy_signal=info.get("ticker") in buy_set)
            if cand is not None:
                scored.append((info, cand))
        scored.sort(key=lambda t: t[1]["score"], reverse=True)
        survivors = scored[: settings.multibagger_deep_top_n]

        # ── Deep stage: statements + promoter holding for the survivors only ──
        _set_job("deep", 80.0, f"Deep-diving top {len(survivors)}")
        sem = asyncio.Semaphore(settings.ingestion_concurrency)

        async def _enrich(info: dict, cand: dict) -> dict:
            async with sem:
                deep = await _fetch_deep(info["ticker"], market)
            if not deep:
                return cand
            merged = {**info, **deep, "_deep": True}
            refined = score_candidate(merged, market, buy_signal=info.get("ticker") in buy_set)
            return refined or cand

        final = await asyncio.gather(*(_enrich(info, cand) for info, cand in survivors))
        final = [c for c in final if c is not None]
        final.sort(key=lambda c: c["score"], reverse=True)
        top = final[:top_n]

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        payload = {
            "market": m.key,
            "universe": _universe_label(m),
            "run_at": started.isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "screened": len(infos),
            "top_n": top_n,
            "count": len(top),
            "candidates": top,
            "weights": _pillar_weights(),
            "disclaimer": DISCLAIMER,
        }
        if persist:
            _persist(payload, market)
        _set_job("done", 100.0, f"{len(top)} candidates")
        job_state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat())
        return payload
    except Exception as e:
        logger.error("Multi-bagger screen failed for %s: %s", market, e)
        job_state.update(running=False, error=str(e),
                         finished_at=datetime.now(timezone.utc).isoformat())
        raise


def _universe_label(m) -> str:
    return {"US": "S&P 500 + NASDAQ-100", "IN": "NIFTY 500"}.get(m.key, m.default_universe)


# ── Tracking ─────────────────────────────────────────────────────────
# Multi-baggers are open-ended long-horizon bets — there's no stop/target to "close" them on (unlike
# the horizon idea tracker in runner.py). So tracking follows the **bagger multiple** (current ÷ entry),
# milestone progress, and **thesis drift** (entry score → a fresh deep re-score) instead of win/loss.
# Tracking is explicit (the user picks conviction names); a screen refresh never auto-tracks.

MB_MILESTONES = [1.5, 2.0, 3.0, 5.0, 10.0]
_MILESTONE_LABEL = {1.5: "1.5×", 2.0: "2× (double bagger)", 3.0: "3×", 5.0: "5×", 10.0: "10× 🚀"}


def _tracked_file(market: str = "IN"):
    return RESULTS_DIR / f"multibagger_tracked_{(market or 'IN').upper()}.json"


def _load_tracked(market: str = "IN") -> list:
    path = _tracked_file(market)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_tracked(market: str, items: list) -> None:
    dest = _tracked_file(market)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    tmp.replace(dest)


def _milestone_label(multiple: Optional[float]) -> Optional[str]:
    """Highest bagger milestone reached (2× = a 'double bagger'), or None if still below 1.5×."""
    if multiple is None:
        return None
    reached = [m for m in MB_MILESTONES if multiple >= m]
    return _MILESTONE_LABEL[max(reached)] if reached else None


def track_multibaggers(market: str = "IN", ticker: Optional[str] = None,
                       top_n: Optional[int] = None) -> dict:
    """Snapshot multi-bagger candidate(s) from the persisted screen into the tracker so their outcome
    can be followed. `ticker` given → track just that one; else track the current top `top_n`.
    Idempotent per (ticker, market) — an already-tracked name is skipped. Returns {added, total}."""
    mk = (market or "IN").upper()
    screen = load_latest_multibaggers(market)
    if not screen or not screen.get("candidates"):
        return {"added": 0, "total": len(_load_tracked(market)),
                "note": "no multi-bagger screen yet — run one first"}
    cands = screen["candidates"]
    if ticker:
        tk = ticker.upper()
        cands = [c for c in cands if (c.get("ticker") or "").upper() == tk]
        if not cands:
            return {"added": 0, "total": len(_load_tracked(market)),
                    "note": f"{ticker} is not in the current {mk} screen"}
    elif top_n:
        cands = cands[: int(top_n)]

    tracked = _load_tracked(market)
    have = {(t.get("ticker") or "").upper() for t in tracked}
    now = datetime.now(timezone.utc).isoformat()
    entry_date = (screen.get("run_at") or now)[:10]
    added = 0
    for c in cands:
        tk = (c.get("ticker") or "").upper()
        if not tk or tk in have:
            continue
        tracked.append({
            "ticker": c["ticker"], "name": c.get("name"), "sector": c.get("sector"),
            "market": mk, "entry_date": entry_date, "entry_price": c.get("price"),
            "entry_score": c.get("score"), "entry_pillars": c.get("pillars"),
            "tracked_at": now,
        })
        have.add(tk)
        added += 1
    _save_tracked(market, tracked)
    return {"added": added, "total": len(tracked)}


def untrack_multibaggers(market: str = "IN", ticker: Optional[str] = None) -> dict:
    """Stop tracking one name (ticker) or all of the market's tracked multi-baggers. Returns {removed}."""
    tracked = _load_tracked(market)
    if ticker is None:
        _save_tracked(market, [])
        return {"removed": len(tracked)}
    tk = ticker.upper()
    keep = [t for t in tracked if (t.get("ticker") or "").upper() != tk]
    _save_tracked(market, keep)
    return {"removed": len(tracked) - len(keep)}


def _mb_scoreboard(rows: list) -> dict:
    """Basket tally over the tracked names: returns, best/avg multiple, and how many hit each milestone."""
    rets = [r["return_pct"] for r in rows if r.get("return_pct") is not None]
    mults = [r["multiple"] for r in rows if r.get("multiple") is not None]
    return {
        "count": len(rows),
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "median_return_pct": round(statistics.median(rets), 2) if rets else None,
        "avg_multiple": round(sum(mults) / len(mults), 2) if mults else None,
        "best_multiple": round(max(mults), 2) if mults else None,
        "worst_return_pct": round(min(rets), 2) if rets else None,
        # {"1.5": n, "2": n, ...} — names at or beyond each bagger milestone.
        "milestones": {f"{m:g}": sum(1 for x in mults if x >= m) for m in MB_MILESTONES},
    }


async def _rescore_current(ticker: str, market: str) -> Optional[float]:
    """A fresh, DEEP-inclusive multi-bagger score for a tracked name (statements re-fetched, 1-day
    cached) so thesis drift compares like-for-like with the entry score. None when unscorable."""
    info = await market_data.aget_stock_info(ticker, market)
    if not info or "error" in info:
        return None
    deep = await _fetch_deep(ticker, market)
    cand = score_candidate({**info, **deep, "_deep": bool(deep)}, market)
    return cand["score"] if cand else None


async def _basket_curve(tracked: list, market: str) -> list:
    """Unrealised equal-weight basket multiple over time: each name's daily close since entry,
    normalised to its entry price, aligned + forward-filled + averaged → [ts_ms, return_pct] points.
    Degrades to [] (curve hides) when history is unavailable. Never raises."""
    if not tracked:
        return []
    max_days = 0
    for t in tracked:
        try:
            d = datetime.fromisoformat(t.get("entry_date", "")).replace(tzinfo=timezone.utc)
            max_days = max(max_days, (datetime.now(timezone.utc) - d).days)
        except Exception:
            max_days = max(max_days, 200)
    period = _period_for_days(max_days + 10)
    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def _hist(t: dict):
        entry = t.get("entry_price")
        if not entry:
            return None
        async with sem:
            df = await market_data.aget_price_history(t["ticker"], period, "1d", (t.get("market") or market))
        if df is None or not len(df):
            return None
        try:
            close = df["Close"].copy()
            if getattr(close.index, "tz", None) is not None:   # align tz-aware/naive frames safely
                close.index = close.index.tz_localize(None)
            ed = t.get("entry_date")
            if ed:
                close = close[close.index >= pd.Timestamp(ed)]
            if not len(close):
                return None
            norm = close / float(entry)          # ≈1.0 at entry → multiple since
            norm.name = t["ticker"]
            return norm
        except Exception:
            return None

    series = [s for s in await asyncio.gather(*(_hist(t) for t in tracked)) if s is not None]
    if not series:
        return []
    try:
        basket = pd.concat(series, axis=1).sort_index().ffill().mean(axis=1)   # equal-weight avg multiple
        out = []
        for ts, v in basket.items():
            try:
                out.append([int(pd.Timestamp(ts).timestamp() * 1000), round((float(v) - 1.0) * 100.0, 2)])
            except Exception:
                continue
        return out
    except Exception:
        return []


def _period_for_days(days: int) -> str:
    """Smallest history window covering `days` since entry (mirrors runner._period_for_days)."""
    if days <= 25:
        return "1mo"
    if days <= 80:
        return "3mo"
    if days <= 175:
        return "6mo"
    if days <= 350:
        return "1y"
    if days <= 700:
        return "2y"
    return "5y"


async def tracked_multibaggers_status(market: str = "IN") -> dict:
    """Live status of the market's tracked multi-baggers: each re-priced to a bagger multiple +
    return-since-entry + milestone, with a fresh thesis re-score (entry → now drift), plus a basket
    scoreboard and unrealised equity curve. Prices/scores are fetched live (cached)."""
    tracked = _load_tracked(market)
    if not tracked:
        return {"tracked": [], "count": 0, "scoreboard": _mb_scoreboard([]),
                "curve": [], "priced_at": None}

    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def _one(t: dict) -> dict:
        tk, mk = t["ticker"], (t.get("market") or market)
        async with sem:
            cur = await market_data.aget_quote(tk, mk)
            cur_score = await _rescore_current(tk, mk)
        entry = t.get("entry_price")
        ret = mult = None
        if cur is not None and entry:
            mult = round(float(cur) / float(entry), 3)
            ret = round((float(cur) / float(entry) - 1.0) * 100.0, 2)
        drift = (round(cur_score - t["entry_score"], 1)
                 if cur_score is not None and t.get("entry_score") is not None else None)
        return {**t,
                "current_price": round_price(cur) if cur is not None else None,
                "return_pct": ret, "multiple": mult, "milestone": _milestone_label(mult),
                "current_score": cur_score, "score_drift": drift}

    rows = await asyncio.gather(*(_one(t) for t in tracked))
    rows.sort(key=lambda r: (r["multiple"] is not None, r["multiple"] or 0), reverse=True)
    curve = await _basket_curve(tracked, market)
    return {
        "tracked": rows, "count": len(rows),
        "scoreboard": _mb_scoreboard(rows), "curve": curve,
        "priced_at": datetime.now(timezone.utc).isoformat(),
    }
