"""
Portfolio construction engine.

`construct_portfolio(spec)` turns a `PortfolioSpec` into a weighted, constraint-checked
`Portfolio`. It unifies two candidate sources behind one pipeline:

  fundamental  → run each selected screening strategy's ScreenFilters through the
                 existing screener (`screen_fn`, default OrchestratorAgent.screen_only)
                 and rank survivors.
  systematic   → pool the current BUY signals of the selected trading strategies from
                 the latest backtest (`runner.load_latest`) and aggregate by stock —
                 more strategies flagging a name = higher conviction.
  blend        → do both; names present in both get a conviction boost.

The heavy collaborators (orchestrator, backtest runner, market data) are injected or
lazily imported, so the engine is fully unit-testable without any network access.

Pipeline: resolve risk → gather candidates → theme filter → score → rank/trim →
fill sectors → size (per-name cap → sector cap → cash buffer) → summarize.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from pipeline_core.portfolio.models import (
    Portfolio,
    PortfolioBasis,
    PortfolioPosition,
    PortfolioSpec,
    RiskProfile,
    WeightingScheme,
)
from pipeline_core.portfolio.risk import resolve_risk
from pipeline_core.portfolio.themes import sector_matches_theme, theme_universe

# screen_fn: async (PipelineRequest) -> list[StockScore]
ScreenFn = Callable[[object], Awaitable[list]]
# info_fn: async (ticker) -> dict (must carry 'sector', optionally 'name'/'price')
InfoFn = Callable[[str], Awaitable[dict]]

# Positions below this final weight are pruned (their weight redeployed to survivors).
MIN_POSITION_WEIGHT = 0.005

PORTFOLIO_DISCLAIMER = (
    "Model portfolio for decision-support research only — not investment advice. "
    "Weights are model targets and any metrics shown are weighted-average estimates "
    "from the constituent strategies, NOT a realized capital-constrained basket backtest. "
    "Systematic/blend baskets inherit the backtest's survivorship bias (built from CURRENT "
    "index constituents). Past performance does not guarantee future results."
)


class PortfolioError(Exception):
    """Construction could not proceed (e.g. systematic basis with no backtest yet)."""


# ── Candidate bookkeeping ──────────────────────────────────────────


def _blank(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "name": None,
        "sector": None,
        "industry": None,
        "price": None,
        "source_strategies": [],
        "categories": set(),
        "fund_strength_parts": [],   # composite_score (or filter_match_pct) per screen
        "fund_strength": None,
        "conviction": 0,             # number of strategies flagging this name
        "win_rates": [],
        "sharpes": [],
        "stop_pcts": [],
        "target_pcts": [],
        "from_fundamental": False,
        "from_systematic": False,
        "score": 0.0,
    }


def _avg(xs: list) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _merge(a: dict, b: dict) -> None:
    """Fold candidate `b` into `a` (same ticker seen from another source)."""
    a["name"] = a["name"] or b["name"]
    a["sector"] = a["sector"] or b["sector"]
    a["industry"] = a["industry"] or b["industry"]
    a["price"] = a["price"] or b["price"]
    a["source_strategies"] += b["source_strategies"]
    a["categories"] |= b["categories"]
    a["fund_strength_parts"] += b["fund_strength_parts"]
    a["conviction"] += b["conviction"]
    a["win_rates"] += b["win_rates"]
    a["sharpes"] += b["sharpes"]
    a["stop_pcts"] += b["stop_pcts"]
    a["target_pcts"] += b["target_pcts"]
    a["from_fundamental"] = a["from_fundamental"] or b["from_fundamental"]
    a["from_systematic"] = a["from_systematic"] or b["from_systematic"]


# ── Candidate sources ──────────────────────────────────────────────


async def _fundamental_candidates(
    strategy_ids: list[str], risk: RiskProfile, universe_index: str, max_pick: int,
    screen_fn: ScreenFn, market: str = "IN",
) -> dict[str, dict]:
    """Run each selected screening strategy and merge ranked survivors by ticker."""
    from pipeline_core.models.schemas import PipelineRequest, ScreenFilters
    from pipeline_core.strategies import get_strategy

    allowed = set(risk.allowed_categories)
    cands: dict[str, dict] = {}
    for sid in strategy_ids:
        strat = get_strategy(sid, market)
        if strat is None or strat.get("category") not in allowed:
            continue
        filters = dict(strat["filters"])
        filters["index"] = universe_index  # risk tier controls the universe
        request = PipelineRequest(
            market=market,
            filters=ScreenFilters.model_validate(filters),
            max_candidates=max_pick,
        )
        scores = await screen_fn(request)
        for s in scores or []:
            c = cands.setdefault(s.ticker, _blank(s.ticker))
            c["name"] = c["name"] or s.name
            c["sector"] = c["sector"] or getattr(s, "sector", None)
            c["industry"] = c["industry"] or getattr(s, "industry", None)
            c["price"] = c["price"] or getattr(s, "price", None)
            c["source_strategies"].append(strat["name"])
            c["categories"].add(strat["category"])
            strength = s.composite_score if s.composite_score is not None else s.filter_match_pct
            if strength is not None:
                c["fund_strength_parts"].append(strength)
            c["conviction"] += 1
            c["from_fundamental"] = True
    return cands


async def _systematic_candidates(
    strategy_ids: list[str], risk: RiskProfile, market: str = "IN"
) -> dict[str, dict]:
    """Pool current BUY signals of the selected trading strategies, aggregated by stock."""
    from pipeline_core.backtest import runner

    latest = runner.load_latest(market)
    if latest is None:
        raise PortfolioError(
            f"No {market} backtest results yet — run a backtest first (systematic/blend basis needs it)."
        )

    allowed = set(risk.allowed_categories)
    by_id = {s["id"]: s for s in latest.get("strategies", [])}
    cands: dict[str, dict] = {}
    for sid in strategy_ids:
        s = by_id.get(sid)
        if s is None or s.get("category") not in allowed:
            continue
        plan = runner._strategy_plan(sid)
        metrics = s.get("metrics", {}) or {}
        for b in (s.get("signals", {}) or {}).get("buy", []):
            tk = b["ticker"]
            c = cands.setdefault(tk, _blank(tk))
            c["price"] = c["price"] or b.get("price")
            c["source_strategies"].append(s["name"])
            c["categories"].add(s["category"])
            c["conviction"] += 1
            c["from_systematic"] = True
            if metrics.get("win_rate") is not None:
                c["win_rates"].append(metrics["win_rate"])
            if metrics.get("sharpe") is not None:
                c["sharpes"].append(metrics["sharpe"])
            for sp in (plan.get("stop_loss_pct"), plan.get("trailing_stop_pct")):
                if sp is not None:
                    c["stop_pcts"].append(sp)
            if plan.get("target_pct") is not None:
                c["target_pcts"].append(plan["target_pct"])
    return cands


# ── Scoring, sizing, constraints ───────────────────────────────────


def _finalize_strength(cands: dict[str, dict]) -> None:
    for c in cands.values():
        c["fund_strength"] = _avg(c["fund_strength_parts"])


def _score(cands: list[dict], basis: PortfolioBasis) -> None:
    """Assign each candidate a ranking `score` (higher = better)."""
    strengths = [c["fund_strength"] for c in cands if c["fund_strength"] is not None]
    convs = [c["conviction"] for c in cands]
    smin, smax = (min(strengths), max(strengths)) if strengths else (0.0, 0.0)
    vmin, vmax = (min(convs), max(convs)) if convs else (0, 0)

    def nrm(x, lo, hi):
        if x is None:
            return 0.0
        return (x - lo) / (hi - lo) if hi > lo else (1.0 if x else 0.0)

    for c in cands:
        if basis == PortfolioBasis.FUNDAMENTAL:
            c["score"] = c["fund_strength"] if c["fund_strength"] is not None else c["conviction"] * 10.0
        elif basis == PortfolioBasis.SYSTEMATIC:
            # conviction dominates; average win-rate breaks ties
            c["score"] = c["conviction"] * 100.0 + (_avg(c["win_rates"]) or 0.0)
        else:  # BLEND
            in_both = c["from_fundamental"] and c["from_systematic"]
            c["score"] = round(
                0.5 * nrm(c["fund_strength"], smin, smax)
                + 0.5 * nrm(c["conviction"], vmin, vmax)
                + (0.15 if in_both else 0.0),
                4,
            )


def _base_weights(cands: list[dict], weighting: WeightingScheme) -> dict[str, float]:
    raw: dict[str, float] = {}
    for c in cands:
        if weighting == WeightingScheme.EQUAL:
            raw[c["ticker"]] = 1.0
        elif weighting == WeightingScheme.CONVICTION:
            raw[c["ticker"]] = float(max(c["conviction"], 1))
        else:  # SCORE
            raw[c["ticker"]] = max(float(c["score"] or 0.0), 1e-6)
    total = sum(raw.values()) or 1.0
    return {k: v / total for k, v in raw.items()}


def _cap_per_name(weights: dict[str, float], cap: float) -> dict[str, float]:
    """Water-fill: clip names above `cap`, redistribute the excess into names with room."""
    weights = dict(weights)
    if cap >= 1.0 or not weights:
        return weights
    for _ in range(200):
        over = [k for k, v in weights.items() if v > cap + 1e-12]
        if not over:
            break
        excess = sum(weights[k] - cap for k in over)
        for k in over:
            weights[k] = cap
        room = {k: cap - v for k, v in weights.items() if v < cap - 1e-12}
        total_room = sum(room.values())
        if total_room <= 1e-12:
            break  # everyone at the cap — remainder falls through to cash
        for k, r in room.items():
            weights[k] += excess * (r / total_room)
    return weights


def _cap_sector(
    weights: dict[str, float], sector_by: dict[str, str], sector_cap: float, per_name_cap: float
) -> dict[str, float]:
    """Scale over-cap sectors down to the cap, redistributing to names with room.
    Skipped by the caller when the basket is effectively single-sector."""
    weights = dict(weights)
    if sector_cap >= 1.0:
        return weights
    for _ in range(50):
        sums: dict[str, float] = {}
        for tk, w in weights.items():
            sums.setdefault(sector_by.get(tk) or "Unknown", 0.0)
            sums[sector_by.get(tk) or "Unknown"] += w
        over = {s: tot for s, tot in sums.items() if tot > sector_cap + 1e-9}
        if not over:
            break
        freed = 0.0
        for s, tot in over.items():
            scale = sector_cap / tot
            for tk in [t for t in weights if (sector_by.get(t) or "Unknown") == s]:
                new = weights[tk] * scale
                freed += weights[tk] - new
                weights[tk] = new
        room = {
            tk: per_name_cap - w
            for tk, w in weights.items()
            if sums.get(sector_by.get(tk) or "Unknown", 0.0) <= sector_cap and w < per_name_cap - 1e-9
        }
        total_room = sum(room.values())
        if total_room <= 1e-9 or freed <= 1e-9:
            break  # nowhere to place freed weight → falls through to cash
        for tk, r in room.items():
            weights[tk] += freed * (r / total_room)
    return weights


async def _ensure_sectors(cands: list[dict], info_fn: InfoFn) -> None:
    """Backfill sector/name/price for candidates missing a sector (systematic names)."""
    missing = [c for c in cands if not c.get("sector")]
    if not missing:
        return
    sem = asyncio.Semaphore(8)

    async def one(c: dict) -> None:
        async with sem:
            try:
                info = await info_fn(c["ticker"])
            except Exception:
                return
            if not info:
                return
            c["sector"] = c["sector"] or info.get("sector")
            c["industry"] = c["industry"] or info.get("industry")
            c["name"] = c["name"] or info.get("name")
            if c["price"] is None:
                c["price"] = info.get("price")

    await asyncio.gather(*(one(c) for c in missing))


# ── Public API ─────────────────────────────────────────────────────


async def construct_portfolio(
    spec: PortfolioSpec,
    *,
    screen_fn: Optional[ScreenFn] = None,
    info_fn: Optional[InfoFn] = None,
) -> Portfolio:
    """Build a weighted, constraint-checked portfolio from a spec.

    Raises `PortfolioError` for unrecoverable problems (e.g. systematic/blend basis
    with no backtest available, or no strategies selected).
    """
    if not spec.strategy_ids:
        raise PortfolioError("Select at least one strategy_id to construct a portfolio.")

    from pipeline_core.markets import get_market

    market = get_market(spec.market)
    risk = resolve_risk(spec.risk, spec.risk_overrides)
    max_positions = int(spec.max_positions or risk.max_positions)
    # The universe tier is market-aware: each market maps risk appetite → universe.
    universe_index = market.cap_tiers.get(risk.appetite.value, risk.cap_tier_index)
    theme_set = theme_universe(spec.theme)
    max_pick = min(max(max_positions * 3, 30), 50)  # PipelineRequest caps max_candidates at 50

    # Lazy defaults for the collaborators (kept out of module import for cheapness/testability).
    if screen_fn is None:
        from pipeline_core.agents.orchestrator import OrchestratorAgent

        screen_fn = OrchestratorAgent().screen_only
    if info_fn is None:
        from pipeline_core.services import market_data

        async def info_fn(ticker: str) -> dict:   # bind market for sector backfill
            return await market_data.aget_stock_info(ticker, market.key)

    # 1. Gather candidates by basis.
    if spec.basis == PortfolioBasis.FUNDAMENTAL:
        cands = await _fundamental_candidates(spec.strategy_ids, risk, universe_index, max_pick, screen_fn, market.key)
    elif spec.basis == PortfolioBasis.SYSTEMATIC:
        cands = await _systematic_candidates(spec.strategy_ids, risk, market.key)
    elif spec.basis == PortfolioBasis.BLEND:
        fund = await _fundamental_candidates(spec.strategy_ids, risk, universe_index, max_pick, screen_fn, market.key)
        syst = await _systematic_candidates(spec.strategy_ids, risk, market.key)
        cands = fund
        for tk, c in syst.items():
            if tk in cands:
                _merge(cands[tk], c)
            else:
                cands[tk] = c
    else:  # pragma: no cover - enum-exhaustive
        raise PortfolioError(f"Unknown basis: {spec.basis}")

    _finalize_strength(cands)

    # 2. Theme tilt (curated basket, widened by a sector-keyword match where sector is known).
    cand_list = list(cands.values())
    if theme_set:
        cand_list = [
            c for c in cand_list
            if c["ticker"] in theme_set or sector_matches_theme(spec.theme, c.get("sector"), c.get("industry"))
        ]

    # 3. Score, rank, trim.
    _score(cand_list, spec.basis)
    cand_list.sort(key=lambda c: (c["score"], c["conviction"], _avg(c["win_rates"]) or 0.0), reverse=True)
    selected = cand_list[:max_positions]

    notes: list[str] = []
    if not selected:
        return _empty_portfolio(spec, risk, "No candidates matched the selected strategies/theme/risk.")

    # 4. Backfill sectors for names still missing one (systematic side), then size.
    await _ensure_sectors(selected, info_fn)
    sector_by = {c["ticker"]: (c.get("sector") or "Unknown") for c in selected}
    distinct_real = {s for s in sector_by.values() if s != "Unknown"}

    base = _base_weights(selected, spec.weighting)   # sums to 1, preserves scheme proportions
    capped = _cap_per_name(base, risk.per_name_cap)
    if len(distinct_real) > 1:
        capped = _cap_sector(capped, sector_by, risk.sector_cap, risk.per_name_cap)
    else:
        notes.append("single-sector basket — sector cap not applied")

    # 5. Deploy the intended capital, scaling to (1 - cash_pct). The per-name/sector caps are
    #    ceilings honored when the opportunity set allows. If capping can't fill the target
    #    (too few qualifying names), the caps would only flatten the basket — so we instead
    #    keep the chosen scheme's proportions, deploy the capital, and disclose the relaxation.
    invested_target = 1.0 - risk.cash_pct
    invested_capped = sum(capped.values())
    if invested_capped >= invested_target - 1e-9:
        weights = {k: v * (invested_target / invested_capped) for k, v in capped.items()}
    else:
        weights = {k: v * invested_target for k, v in base.items()}
        notes.append(
            f"per-name/sector caps relaxed — only {len(selected)} qualifying name(s); "
            "weights follow the chosen scheme and capital is fully deployed"
        )

    # Prune negligible positions (e.g. a score-0 tail under score weighting) and redeploy
    # their weight across the survivors, so the basket has no dead 0% holdings.
    kept = {k: v for k, v in weights.items() if v >= MIN_POSITION_WEIGHT}
    if kept and len(kept) < len(weights):
        total = sum(kept.values())
        weights = {k: v * (invested_target / total) for k, v in kept.items()} if total > 0 else kept
        selected = [c for c in selected if c["ticker"] in weights]
        sector_by = {tk: sector_by[tk] for tk in weights}
    cash_pct = round(1.0 - sum(weights.values()), 4)

    if len(distinct_real) > 1:
        _sector_final: dict[str, float] = {}
        for tk, w in weights.items():
            _sector_final[sector_by[tk]] = _sector_final.get(sector_by[tk], 0.0) + w
        if any(v > risk.sector_cap + 1e-6 for s, v in _sector_final.items() if s != "Unknown"):
            notes.append("sector concentration above cap — limited diversification opportunity")

    # 6. Build positions + weighted-from-constituent metrics (still have candidate dicts here).
    positions = [_to_position(c, round(weights[c["ticker"]], 4), risk) for c in selected]
    positions.sort(key=lambda p: p.weight, reverse=True)

    wpairs = [(weights[c["ticker"]], c) for c in selected]
    metrics = {
        "weighted_win_rate": _weighted_cand(wpairs, lambda c: _avg(c["win_rates"])),
        "weighted_sharpe": _weighted_cand(wpairs, lambda c: _avg(c["sharpes"])),
        "weighted_fundamental_strength": _weighted_cand(wpairs, lambda c: c["fund_strength"]),
    }
    summary = _summarize(spec, risk, positions, cash_pct, notes, metrics)
    return Portfolio(
        name=spec.name,
        spec=spec,
        positions=positions,
        cash_pct=cash_pct,
        summary=summary,
        disclaimer=PORTFOLIO_DISCLAIMER,
    )


def _to_position(c: dict, weight: float, risk: RiskProfile) -> PortfolioPosition:
    entry = c.get("price")
    if c["stop_pcts"]:
        stop_pct, stop_source = round(min(c["stop_pcts"]), 4), "strategy"
    else:
        stop_pct, stop_source = risk.default_stop_pct, "risk-default"
    stop_price = round(entry * (1 - stop_pct), 2) if entry else None
    target_pct = round(max(c["target_pcts"]), 4) if c["target_pcts"] else None
    srcs = list(dict.fromkeys(c["source_strategies"]))  # de-dupe, keep order
    rationale = f"{c['conviction']}× signal · " + ", ".join(srcs[:3]) + ("…" if len(srcs) > 3 else "")
    return PortfolioPosition(
        ticker=c["ticker"],
        name=c.get("name"),
        sector=c.get("sector"),
        weight=weight,
        conviction=c["conviction"],
        source_strategies=srcs,
        entry_price=entry,
        stop_pct=stop_pct,
        stop_price=stop_price,
        stop_source=stop_source,
        target_pct=target_pct,
        score=round(float(c["score"]), 4),
        rationale=rationale,
    )


def _summarize(spec, risk, positions: list[PortfolioPosition], cash_pct: float, notes: list[str], metrics: dict) -> dict:
    alloc_sector: dict[str, float] = {}
    strategy_coverage: dict[str, int] = {}
    for p in positions:
        key = p.sector or "Unknown"
        alloc_sector[key] = round(alloc_sector.get(key, 0.0) + p.weight, 4)
        for s in p.source_strategies:
            strategy_coverage[s] = strategy_coverage.get(s, 0) + 1

    top3 = round(sum(sorted((p.weight for p in positions), reverse=True)[:3]), 4)
    largest = max(positions, key=lambda p: p.weight)
    return {
        "basis": spec.basis.value,
        "theme": spec.theme,
        "risk": risk.appetite.value,
        "weighting": spec.weighting.value,
        "universe_index": risk.cap_tier_index,
        "position_count": len(positions),
        "invested_pct": round(1.0 - cash_pct, 4),
        "cash_pct": cash_pct,
        "largest_position": {"ticker": largest.ticker, "weight": largest.weight},
        "top3_concentration": top3,
        "sector_count": len({p.sector or "Unknown" for p in positions}),
        "allocation_by_sector": dict(sorted(alloc_sector.items(), key=lambda kv: kv[1], reverse=True)),
        "strategy_coverage": dict(sorted(strategy_coverage.items(), key=lambda kv: kv[1], reverse=True)),
        "avg_conviction": round(_avg([p.conviction for p in positions]) or 0.0, 2),
        **{k: v for k, v in metrics.items() if v is not None},
        "notes": notes,
    }


def _weighted_cand(pairs, fn) -> Optional[float]:
    """Weight-average `fn(candidate)` over (weight, candidate) pairs, skipping Nones."""
    num = den = 0.0
    for w, c in pairs:
        v = fn(c)
        if v is not None:
            num += w * v
            den += w
    return round(num / den, 3) if den > 0 else None


def _empty_portfolio(spec: PortfolioSpec, risk: RiskProfile, reason: str) -> Portfolio:
    return Portfolio(
        name=spec.name,
        spec=spec,
        positions=[],
        cash_pct=1.0,
        summary={
            "basis": spec.basis.value,
            "theme": spec.theme,
            "risk": risk.appetite.value,
            "position_count": 0,
            "notes": [reason],
        },
        disclaimer=PORTFOLIO_DISCLAIMER,
    )
