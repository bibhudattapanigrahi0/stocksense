"""
Conviction / win-rate calibration.

A trade idea advertises a win probability (the flagging strategies' average backtest win-rate),
but do ideas that *say* 58% actually win 58% of the time? This module answers that from the trade
ledger — the realized outcomes of past tracked ideas — and fits a monotonic **isotonic** mapping
from the advertised win-rate to the realized one. That calibrated probability then feeds position
sizing (`pipeline_core/sizing.py` prefers `calibrated_win_pct`), so allocations reflect measured
hit-rates rather than an optimistic backtest number.

Two views are produced:
  • a reliability curve (isotonic) — advertised win-rate → realized win-rate, used by the sizer;
  • a reliability table by conviction — does "more strategies agree" really mean higher win-rate?

Dependency-free (pure Python + the standard lib): isotonic regression is a small
pool-adjacent-violators (PAV) routine. Degrades gracefully — too few closed trades → no curve →
callers keep the advertised win-rate unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


def pav_isotonic(y: list[float], w: list[float]) -> list[float]:
    """Weighted isotonic (non-decreasing) fit via pool-adjacent-violators. Inputs must already be
    sorted by x ascending; returns fitted values aligned to that order."""
    val: list[float] = []
    wt: list[float] = []
    cnt: list[int] = []
    for yi, wi in zip(y, w):
        val.append(yi)
        wt.append(wi)
        cnt.append(1)
        while len(val) > 1 and val[-2] > val[-1]:      # pool the last two blocks that violate order
            tw = wt[-1] + wt[-2]
            mv = (val[-1] * wt[-1] + val[-2] * wt[-2]) / tw if tw > 0 else (val[-1] + val[-2]) / 2
            tc = cnt[-1] + cnt[-2]
            val[-2:], wt[-2:], cnt[-2:] = [mv], [tw], [tc]
    out: list[float] = []
    for v, c in zip(val, cnt):
        out.extend([v] * c)
    return out


def _apply_knots(knots: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation across (x, calibrated) knots, clamped at the ends."""
    if not knots:
        return x
    if x <= knots[0][0]:
        return knots[0][1]
    if x >= knots[-1][0]:
        return knots[-1][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return knots[-1][1]


def fit_calibration(samples: list[tuple[float, bool]], *, min_samples: int = 20) -> Optional[dict]:
    """Fit an isotonic advertised→realized win-rate curve from (predicted_fraction, won) samples.
    Returns {knots, n, method, brier_raw, brier_cal} or None if there aren't enough decided trades
    (so the caller falls back to the advertised rate)."""
    decided = [(float(p), 1.0 if won else 0.0) for p, won in samples if p is not None]
    if len(decided) < min_samples:
        return None
    decided.sort(key=lambda t: t[0])
    xs = [d[0] for d in decided]
    ys = [d[1] for d in decided]
    fitted = pav_isotonic(ys, [1.0] * len(ys))

    knots: list[tuple[float, float]] = []
    for x, f in zip(xs, fitted):
        if knots and abs(knots[-1][0] - x) < 1e-9:
            knots[-1] = (x, f)                 # same predicted value → keep the pooled level
        else:
            knots.append((x, f))

    brier_raw = sum((p - y) ** 2 for p, y in decided) / len(decided)
    brier_cal = sum((_apply_knots(knots, p) - y) ** 2 for p, y in decided) / len(decided)
    return {
        "knots": [[round(x, 4), round(y, 4)] for x, y in knots],
        "n": len(decided), "method": "isotonic",
        "brier_raw": round(brier_raw, 4), "brier_cal": round(brier_cal, 4),
    }


def apply_calibration(curve: Optional[dict], predicted_pct: float) -> float:
    """Map an advertised win-rate (percentage, 0–100) through the fitted curve to a calibrated
    percentage. No curve → returns the input unchanged."""
    p = float(predicted_pct)
    frac = p / 100.0 if p > 1.0 else p
    if not curve or not curve.get("knots"):
        return round(max(0.0, min(100.0, p if p > 1.0 else p * 100.0)), 1)
    knots = [(k[0], k[1]) for k in curve["knots"]]
    cal = _apply_knots(knots, frac) * 100.0
    return round(max(0.0, min(100.0, cal)), 1)


def reliability_by_conviction(rows: dict[int, list[int]]) -> list[dict]:
    """rows: conviction → [wins, n]. Returns a sorted realized-win-rate table."""
    out = []
    for conv, (wins, n) in sorted(rows.items()):
        out.append({"conviction": conv, "n": n,
                    "realized_win_pct": round(wins / n * 100, 1) if n else None})
    return out


def _is_win(idea: dict) -> Optional[bool]:
    r = idea.get("realized_return_pct")
    return None if r is None else (r > 0)


def build_from_ledger(market: str, *, min_samples: int = 20) -> dict:
    """Build calibration for a market from the closed tracked ideas (the trade ledger). Returns the
    fitted curve (or None), a reliability-by-conviction table, and sample counts."""
    from pipeline_core.backtest import runner  # lazy: avoid an import cycle

    mk = (market or "IN").upper()
    closed = [t for t in runner._load_tracked()
              if t.get("closed") and (t.get("market") or "IN").upper() == mk]

    pred_samples: list[tuple[float, bool]] = []
    conv_rows: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for t in closed:
        won = _is_win(t)
        if won is None:
            continue
        pred = t.get("avg_win_rate")
        if pred is not None:
            pred_samples.append((pred / 100.0, won))
        conv = t.get("conviction")
        if conv is not None:
            conv_rows[int(conv)][0] += 1 if won else 0
            conv_rows[int(conv)][1] += 1

    curve = fit_calibration(pred_samples, min_samples=min_samples)
    return {
        "market": mk,
        "curve": curve,
        "active": curve is not None,
        "reliability_by_conviction": reliability_by_conviction(conv_rows),
        "closed": len(closed),
        "decided": len(pred_samples),
        "min_samples": min_samples,
    }
