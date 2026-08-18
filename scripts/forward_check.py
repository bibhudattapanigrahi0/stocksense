#!/usr/bin/env python3
"""Forward validation of the edge-repair changes, on the LIVE local ledger.

The ranking/gate changes (edge-weighted conviction, breakeven edge floor, cohort demote, oversold
entry gate) are FORWARD-looking — they change what the board surfaces and sizes, not already-closed
trades. Their real proof accrues as new ideas close. Those outcomes live only in the local ledger
(data/backtests/tracked_ideas.json, gitignored), so this check must run locally, not in a cloud job.

Run it every couple of weeks:

    python scripts/forward_check.py

Reports, over closed EQUITY (US/IN) trades:
  1. Oversold entry gate — realized win% / net by oversold vs "entered extended" cohort
  2. Conviction rank IC — Spearman of raw vs edge-weighted conviction against realized net return
  3. Net cohort stats by market / horizon

Records tracked BEFORE the changes lack the entry-state fields (weighted_conviction / oversold);
they're excluded from the forward cohort so the signal stays clean as new trades accumulate.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / "data" / "backtests" / "tracked_ideas.json"


def _spearman(xs, ys):
    """Rank correlation with average-rank tie handling; None if too few points or no variance."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1                       # 1-based average rank across the tie block
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else None


def _win(rows):
    return 100 * sum(1 for r in rows if r["realized_return_pct"] > 0) / len(rows) if rows else 0.0


def _mean(rows):
    return sum(r["realized_return_pct"] for r in rows) / len(rows) if rows else 0.0


def main() -> int:
    if not LEDGER.exists():
        print(f"No ledger at {LEDGER}")
        return 1
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    eq = [r for r in data
          if str(r.get("market")) in ("US", "IN") and r.get("closed")
          and r.get("realized_return_pct") is not None and r.get("side", "long") != "short"]
    if not eq:
        print("No closed equity trades yet.")
        return 0
    fwd = [r for r in eq if r.get("oversold") is not None]        # tracked under the new entry-state
    print(f"Closed equity trades: {len(eq)}  "
          f"(forward w/ entry-state: {len(fwd)}, legacy: {len(eq) - len(fwd)})\n")

    print("1) OVERSOLD ENTRY GATE — realized by cohort (forward records only)")
    if fwd:
        ov = [r for r in fwd if r.get("oversold")]
        ext = [r for r in fwd if not r.get("oversold")]
        print(f"   kept clean (oversold)  n={len(ov):>3}  win%={_win(ov):5.1f}  net={_mean(ov):+.2f}")
        print(f"   demoted (extended)     n={len(ext):>3}  win%={_win(ext):5.1f}  net={_mean(ext):+.2f}")
        print("   → healthy if oversold win% >> extended win% (validation target ~74 vs ~34)")
    else:
        print("   (no forward records with entry-state yet — check back after more ideas close)")

    print("\n2) CONVICTION RANK IC — raw vs edge-weighted (Spearman vs realized net; higher = better ranker)")
    ic_raw = _spearman([r["conviction"] for r in eq if r.get("conviction") is not None],
                       [r["realized_return_pct"] for r in eq if r.get("conviction") is not None])
    wtd = [r for r in fwd if r.get("weighted_conviction") is not None]
    if ic_raw is not None:
        print(f"   raw conviction    n={sum(1 for r in eq if r.get('conviction') is not None):>3}  "
              f"IC={ic_raw:+.3f}   (was ~-0.31 pre-fix; harmful if negative)")
    if len(wtd) >= 3:
        ic_w = _spearman([r["weighted_conviction"] for r in wtd],
                         [r["realized_return_pct"] for r in wtd])
        print(f"   edge-weighted     n={len(wtd):>3}  IC={ic_w:+.3f}   (validation target positive)")
    else:
        print("   edge-weighted     (no forward records with weighted_conviction yet)")

    print("\n3) NET COHORT STATS by market / horizon")
    by = defaultdict(list)
    for r in eq:
        by[(r["market"], r["horizon"])].append(r)
    for k in sorted(by):
        rows = by[k]
        print(f"   {k[0]:<3} {k[1]:<7} n={len(rows):>3}  win%={_win(rows):5.1f}  net={_mean(rows):+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
