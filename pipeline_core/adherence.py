"""
Adherence — "system vs you": did sticking to the plan beat what you actually did?

The pipeline already tracks what each idea's PLAN did (the trade ledger closes on stop/target/expiry
with a realized return). This measures the gap between that and the user's OWN behaviour — the single
most motivating discipline signal: *"following the plan you'd be +8.3%; your early exits cost 6.2%."*

Comparison model — **exit discipline with the entry held constant** at the plan's entry. For each idea
the user acted on we compare YOUR exit price to the PLAN's exit price (stop / target / expiry, or a live
mark if neither has hit). Holding entry constant isolates the one decision that matters: when you got
out. `delta = (your_exit − plan_exit) / entry`; negative = an early exit that COST you (left money before
the target), positive = one that SAVED you (bailed above a stop that later hit).

Two capture inputs feed the same scorecard:
  • explicit — a `user_action` (I'm-in / I'm-out) recorded on a tracked idea; the plan side is the tracked
    idea's own ledger outcome (already computed by `runner.tracked_ideas_status`).
  • paper — an idea/basket/multibagger-sourced paper trade (it embeds the plan: stop/target); the plan
    side is that plan replayed over the price path via `runner._evaluate_idea_path`.

The scoring core (top half) is pure/injected-price and unit-testable; the async orchestrator (bottom
half) loads the ledger + paper book and fetches history, mirroring `paper.py`'s split.
"""

from __future__ import annotations

import asyncio
from math import prod
from typing import Optional

FOLLOWED_EPS = 0.5      # |delta_pct| within this ⇒ you exited ≈ where the plan did → "followed"
_PAPER_SOURCES = ("idea", "basket", "multibagger")   # paper sources that carry an idea's plan


# ── Pure scoring core ───────────────────────────────────────────────

def _pct(entry: Optional[float], exit_: Optional[float], side: str = "long") -> Optional[float]:
    """Return % from entry to exit, or None if unpriceable. A SHORT earns the inverse move."""
    if not entry or exit_ is None or entry <= 0:
        return None
    sign = -1 if side == "short" else 1
    return round(sign * (exit_ - entry) / entry * 100, 2)


def _compound(rets: list) -> Optional[float]:
    """Compound a list of per-trade returns (%) into a book return (%). None if empty."""
    vals = [r for r in rets if r is not None]
    if not vals:
        return None
    return round((prod(1 + r / 100.0 for r in vals) - 1) * 100, 2)


def exit_delta(entry: Optional[float], your_exit: Optional[float], plan_exit: Optional[float],
               side: str = "long") -> dict:
    """Compare your exit to the plan's exit, entry held constant. Returns {delta_pct, verdict}.
    verdict: 'followed' (≈ the plan), 'early_cost' (you got out worse than the plan), 'early_saved'
    (better than it), or 'unknown' when unpriceable. For a SHORT the P&L sign flips — covering ABOVE
    the plan's exit costs you — so `delta` is measured on the short's return, not the raw price move."""
    if not entry or your_exit is None or plan_exit is None or entry <= 0:
        return {"delta_pct": None, "verdict": "unknown"}
    sign = -1 if side == "short" else 1
    delta = round(sign * (your_exit - plan_exit) / entry * 100, 2)
    if abs(delta) < FOLLOWED_EPS:
        verdict = "followed"
    elif delta < 0:
        verdict = "early_cost"
    else:
        verdict = "early_saved"
    return {"delta_pct": delta, "verdict": verdict}


def build_row(*, ticker: str, horizon: Optional[str], source: str, entry: Optional[float],
              your_exit: Optional[float], plan_exit: Optional[float], plan_reason: Optional[str],
              acted_out: bool, side: str = "long") -> dict:
    """One comparison row for the scorecard. `acted_out` False (you're still holding / following the
    plan) ⇒ verdict 'followed', delta 0 — an ongoing hold is neither credited nor penalised. `side`
    flips the P&L / delta sign for shorts."""
    if not acted_out:
        return {"ticker": ticker, "horizon": horizon, "source": source, "entry": entry, "side": side,
                "your_exit": your_exit, "plan_exit": plan_exit, "plan_reason": plan_reason,
                "your_return_pct": _pct(entry, your_exit, side), "plan_return_pct": _pct(entry, plan_exit, side),
                "delta_pct": 0.0, "verdict": "followed", "acted_out": False}
    d = exit_delta(entry, your_exit, plan_exit, side)
    return {"ticker": ticker, "horizon": horizon, "source": source, "entry": entry, "side": side,
            "your_exit": your_exit, "plan_exit": plan_exit, "plan_reason": plan_reason,
            "your_return_pct": _pct(entry, your_exit, side), "plan_return_pct": _pct(entry, plan_exit, side),
            "delta_pct": d["delta_pct"], "verdict": d["verdict"], "acted_out": True}


def scorecard(rows: list) -> dict:
    """Aggregate comparison rows into the discipline scorecard: compounded 'you vs the plan' book
    returns, the net discipline delta, and the cost/saved split of early exits."""
    cost = [r for r in rows if r["verdict"] == "early_cost"]
    saved = [r for r in rows if r["verdict"] == "early_saved"]
    your_ret = _compound([r["your_return_pct"] for r in rows])
    plan_ret = _compound([r["plan_return_pct"] for r in rows])
    disc = (round(your_ret - plan_ret, 2)
            if your_ret is not None and plan_ret is not None else None)
    return {
        "n": len(rows),
        "acted": sum(1 for r in rows if r.get("acted_out")),
        "followed": sum(1 for r in rows if r["verdict"] == "followed"),
        "early_cost_n": len(cost),
        "early_saved_n": len(saved),
        "your_return_pct": your_ret,
        "plan_return_pct": plan_ret,
        "discipline_delta_pct": disc,        # your book − plan book (negative = discipline cost you)
        "cost_pct": round(sum(r["delta_pct"] for r in cost), 2),      # summed drag of cost exits (≤0)
        "saved_pct": round(sum(r["delta_pct"] for r in saved), 2),   # summed benefit of saved exits (≥0)
    }


def _horizon_from_source(source: Optional[str]) -> Optional[str]:
    """`idea-short` → `short`; basket/multibagger/manual → None (default time-stop applies)."""
    if source and source.startswith("idea-"):
        return source.split("-", 1)[1] or None
    return None


# ── Async orchestrator (ledger + paper book + history) ──────────────

async def discipline_scorecard(market: Optional[str] = None, user_id: Optional[str] = None) -> dict:
    """Assemble the discipline scorecard for a market from BOTH capture paths and score it.
    Network-light: the tracked path reuses `tracked_ideas_status` (already priced); the paper path
    replays each idea-sourced plan over fetched history. Returns {market, rows, scorecard}."""
    from pipeline_core.backtest import runner       # lazy: runner pulls in heavy deps
    from pipeline_core.services import run_store

    mk = market.upper() if market else None
    rows: list = []

    # 1) Explicit (tracked) path — user_action on a tracked idea vs its own ledger outcome.
    status = await runner.tracked_ideas_status(mk)
    for it in status.get("open", []) + status.get("closed", []):
        ua = it.get("user_action") or {}
        if not ua.get("entered"):
            continue
        entry = it.get("entry_price")
        plan_exit = it.get("exit_price") if it.get("closed") else it.get("current_price")
        acted_out = bool(ua.get("exited"))
        your_exit = ua.get("exit_price") if acted_out else it.get("current_price")
        rows.append(build_row(
            ticker=it.get("ticker"), horizon=it.get("horizon"), source="tracked",
            entry=entry, your_exit=your_exit, plan_exit=plan_exit,
            plan_reason=it.get("exit_reason"), acted_out=acted_out, side=it.get("side", "long")))

    # 2) Paper path — idea/basket/multibagger-sourced paper trades vs the plan replayed on price history.
    acct = await run_store.get_paper(mk or "IN", user_id=user_id) if mk else None
    if acct:
        paper_mk = acct.get("market", mk)
        idea_pos = [p for p in (acct.get("positions", []) + acct.get("closed", []))
                    if str(p.get("source", "")).split("-", 1)[0] in _PAPER_SOURCES
                    and p.get("stop") and p.get("target")]
        # Replay each plan over history from its open date, concurrently.
        sem = asyncio.Semaphore(8)

        async def _plan_exit(pos: dict):
            opened = (pos.get("opened_at") or "")[:10]
            days = runner._idea_age_days({"signal_date": opened})
            async with sem:
                df = await asyncio.to_thread(runner.get_price_history, pos["ticker"],
                                             runner._period_for_days(days + 10), "1d", paper_mk)
            pseudo = {"entry_price": pos["entry_price"], "stop_price": pos["stop"],
                      "target_price": pos["target"], "signal_date": opened,
                      "side": pos.get("side", "long"),      # replay the short's stop/target correctly
                      "horizon": _horizon_from_source(pos.get("source")) or "medium"}
            outcome = runner._evaluate_idea_path(pseudo, df)
            if outcome:                                   # plan closed on stop/target/time
                return outcome["exit_price"], outcome["exit_reason"]
            live = await asyncio.to_thread(runner._current_price, pos["ticker"], paper_mk)
            return live, "open"                           # plan still running → mark to live

        plan_exits = await asyncio.gather(*(_plan_exit(p) for p in idea_pos)) if idea_pos else []
        for pos, (plan_exit, plan_reason) in zip(idea_pos, plan_exits):
            closed = "exit_price" in pos                  # a closed paper trade carries an exit
            reason = pos.get("reason")
            # You "acted" only on a MANUAL early close; a stop/target auto-close = you let the plan run.
            acted_out = closed and reason == "manual"
            your_exit = pos.get("exit_price") if closed else plan_exit  # still open ⇒ tracking the plan
            rows.append(build_row(
                ticker=pos.get("ticker"), horizon=_horizon_from_source(pos.get("source")),
                source=pos.get("source", "paper"), entry=pos.get("entry_price"),
                your_exit=your_exit, plan_exit=plan_exit, plan_reason=plan_reason,
                acted_out=acted_out, side=pos.get("side", "long")))

    return {"market": mk, "rows": rows, "scorecard": scorecard(rows)}
