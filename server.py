"""
StockSense MCP Server — exposes the agentic stock screening/analysis
pipeline as MCP tools and resources for LLM clients (Claude Desktop, etc.).

Run (stdio, for Claude Desktop):   python server.py
Run (HTTP, for remote clients):    python server.py --http --port 8001

All tools are read-only and return structured JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from typing import Optional

from fastmcp import FastMCP

from pipeline_core.agents.data_ingestion import DataIngestionAgent
from pipeline_core.agents.orchestrator import OrchestratorAgent, active_runs
from pipeline_core.agents.screener import ScreenerAgent
from pipeline_core.config import REPORTS_DIR, settings
from pipeline_core.models.schemas import (
    AnalysisDepth,
    PipelineRequest,
    PipelineStatus,
    SavedScreen,
    ScreenFilters,
)
from pipeline_core import alerts, paper
from pipeline_core.services import llm_service, market_data, run_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("stocksense.mcp")

mcp = FastMCP(
    "StockSense",
    instructions=(
        "Agentic stock screening and multi-agent analysis for Indian equities "
        "(NSE/BSE). Use screen_stocks for filtering, analyze_stock for a deep "
        "dive on one ticker, and run_pipeline + get_pipeline_status for "
        "universe-scale jobs. For strategy backtesting, use "
        "list_backtest_strategies and get_backtest_leaderboard to compare 35 "
        "trading strategies over 10y of Nifty 500 history, and "
        "get_backtest_strategy / get_backtest_signals to see which stocks are "
        "flagged BUY or SELL under each strategy (run_backtest to refresh). "
        "For portfolio management, use construct_portfolio to build a weighted, "
        "risk-shaped, theme-tilted basket from one or more strategies (see "
        "list_risk_profiles and list_portfolio_themes), then save/list/get and "
        "rebalance_portfolio to manage it over time. "
        "Outputs are decision-support research, not investment advice."
    ),
)

_orchestrator = OrchestratorAgent()
_ingestion = DataIngestionAgent()
_screener = ScreenerAgent()
_background_tasks: dict[str, asyncio.Task] = {}


# ── Tools ──────────────────────────────────────────────────────────


@mcp.tool
async def screen_stocks(
    query: Optional[str] = None,
    filters: Optional[ScreenFilters] = None,
    max_candidates: int = 10,
    market: str = "IN",
) -> dict:
    """
    Screen a market against structured filters and/or a natural-language query
    (e.g. "large cap IT stocks with ROE above 20% and low debt"). Returns ranked
    candidates with per-filter pass/fail explanations.

    market: "IN" (India NSE/BSE), "US" (NYSE/NASDAQ), "METALS" or "CRYPTO".
    Equity markets (IN/US) support valuation/growth/profitability/health filters;
    price-only markets (metals/crypto) support only technical filters. India-
    specific promoter/pledge/delivery filters apply to IN only. Market cap units
    are INR crore (IN) or USD billions (US/crypto).
    """
    request = PipelineRequest(
        market=market,
        query=query,
        filters=filters or ScreenFilters(),
        max_candidates=max(1, min(max_candidates, 50)),
        include_report=False,
    )
    results = await _orchestrator.screen_only(request)
    return {
        "market": market,
        "query": query,
        "filters_applied": request.filters.model_dump(exclude_unset=True),
        "candidates": [r.model_dump() for r in results],
        "count": len(results),
        "llm_mode": llm_service.llm_mode(),
    }


@mcp.tool
async def analyze_stock(ticker: str, analysis_depth: str = "standard", market: str = "IN") -> dict:
    """
    Deep-dive one ticker with the analysis agents fused into a composite score
    (0–100) with the evidence behind each sub-score. Equity markets (IN/US) use
    fundamental + technical + sentiment + risk; price-only markets (metals/crypto)
    use a technical/momentum composite (no fundamentals).

    ticker: e.g. "INFY" (IN), "AAPL" (US), "GC=F"/"GLD" (metals), "BTC" (crypto).
    market: "IN" | "US" | "METALS" | "CRYPTO".
    analysis_depth: "quick" | "standard" | "deep".
    """
    depth = analysis_depth if analysis_depth in ("quick", "standard", "deep") else "standard"
    return await _orchestrator.analyze_ticker(ticker.upper().strip(), depth, market=market)


@mcp.tool
async def get_stock_overview(ticker: str, market: str = "IN") -> dict:
    """
    Fast profile + technicals snapshot for one ticker (no LLM, no financial
    statements): profile, key ratios, NSE promoter/delivery data (IN only),
    and basic technical indicators. market: "IN" | "US" | "METALS" | "CRYPTO".
    """
    symbol = ticker.upper().strip()
    info = await _ingestion.fetch_stock_data(symbol, enrich_nse=True, market=market)
    if "error" in info:
        return {"ticker": symbol, "error": info["error"]}

    df = await market_data.aget_price_history(symbol, period="1y", interval="1d", market=market)
    technicals = _screener.compute_technicals(df)

    return {
        "ticker": symbol,
        "market": market,
        "profile": {k: v for k, v in info.items() if v is not None},
        "technicals": technicals or {},
    }


@mcp.tool
async def run_pipeline(
    query: Optional[str] = None,
    filters: Optional[ScreenFilters] = None,
    analysis_depth: str = "standard",
    max_candidates: int = 10,
    include_report: bool = True,
    market: str = "IN",
) -> dict:
    """
    Start a full pipeline run in the background (screen + deep-dive analysis
    + markdown reports for top picks). Returns a run_id to poll with
    get_pipeline_status. Use this for universe-scale jobs (NIFTY 500, S&P 500…).
    market: "IN" | "US" | "METALS" | "CRYPTO".
    """
    depth = (
        AnalysisDepth(analysis_depth)
        if analysis_depth in ("quick", "standard", "deep")
        else AnalysisDepth.STANDARD
    )
    request = PipelineRequest(
        market=market,
        query=query,
        filters=filters or ScreenFilters(),
        analysis_depth=depth,
        max_candidates=max(1, min(max_candidates, 50)),
        include_report=include_report,
    )

    run_id = str(uuid.uuid4())
    task = asyncio.create_task(_orchestrator.execute(request, run_id=run_id))
    _background_tasks[run_id] = task
    return {
        "run_id": run_id,
        "status": "running",
        "poll_with": f"get_pipeline_status(run_id='{run_id}')",
    }


@mcp.tool
async def get_pipeline_status(run_id: str) -> dict:
    """Poll status, progress, and (when finished) ranked results of a pipeline run."""
    run = active_runs.get(run_id) or await run_store.get_run(run_id)
    if run is None:
        return {"run_id": run_id, "error": "run not found"}

    payload = run.model_dump()
    if run.status == PipelineStatus.RUNNING:
        payload["results"] = []  # keep mid-run polls light
    return payload


@mcp.tool
async def list_pipeline_runs(limit: int = 10) -> dict:
    """List recent pipeline runs (newest first), without result payloads."""
    runs = await run_store.list_runs(limit=max(1, min(limit, 50)))
    return {
        "runs": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "status": r.status.value,
                "query": r.query,
                "candidates_found": r.candidates_found,
                "report_files": r.report_files,
                "error": r.error,
            }
            for r in runs
        ]
    }


@mcp.tool
async def generate_report(ticker: str, market: str = "IN") -> dict:
    """
    Generate a markdown research report for one ticker: runs the analysis fresh,
    renders the report, saves it to disk, and returns the markdown content plus
    filename. market: "IN" | "US" | "METALS" | "CRYPTO".
    """
    symbol = ticker.upper().strip()
    analysis = await _orchestrator.analyze_ticker(symbol, "standard", market=market)
    if "error" in analysis:
        return analysis

    from pipeline_core.agents.report_generator import ReportGeneratorAgent

    report_md = await ReportGeneratorAgent().generate_markdown_report(
        symbol=symbol,
        name=analysis.get("name", symbol),
        fundamental=analysis["evidence"]["fundamental"],
        technical=analysis["evidence"]["technical"],
        sentiment=analysis["evidence"]["sentiment"],
        risk=analysis["evidence"]["risk"],
        composite_score=analysis.get("composite_score") or 50.0,
    )
    filename = f"{symbol}_adhoc.md"
    (REPORTS_DIR / filename).write_text(report_md, encoding="utf-8")
    return {"ticker": symbol, "filename": filename, "markdown": report_md}


@mcp.tool
async def save_screen(name: str, filters: ScreenFilters, description: Optional[str] = None) -> dict:
    """Save a named filter preset for reuse with screen_stocks/run_pipeline."""
    screen = SavedScreen(name=name, description=description, filters=filters)
    saved = await run_store.save_screen(screen)
    return {"id": saved.id, "name": saved.name, "created_at": saved.created_at.isoformat()}


@mcp.tool
async def list_saved_screens() -> dict:
    """List all saved filter presets."""
    screens = await run_store.list_screens()
    return {
        "screens": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "filters": s.filters.model_dump(exclude_unset=True),
            }
            for s in screens
        ]
    }


@mcp.tool
async def get_saved_screen(name: str) -> dict:
    """Load a saved filter preset by name or id."""
    screen = await run_store.get_screen(name)
    if screen is None:
        return {"error": f"no saved screen named '{name}'"}
    return screen.model_dump()


@mcp.tool
async def delete_saved_screen(name: str) -> dict:
    """Delete a saved filter preset by name or id."""
    deleted = await run_store.delete_screen(name)
    return {"deleted": deleted, "name": name}


@mcp.tool
async def list_markets() -> dict:
    """
    List supported markets (IN, US, METALS, CRYPTO) with their selectable
    universes, benchmark, market-cap units, and whether fundamentals apply.
    Pass the market key to screen_stocks / analyze_stock / run_backtest.
    """
    from pipeline_core.markets import list_markets as _list

    return {"markets": _list()}


@mcp.tool
async def get_index_constituents_list(index: str = "NIFTY 50", market: str = "IN") -> dict:
    """List the current constituents of a universe (NIFTY/S&P 500/NASDAQ-100/
    metals/crypto top-N) for the given market."""
    tickers = await asyncio.to_thread(market_data.get_index_tickers, index, market)
    return {
        "market": market,
        "index": index,
        "count": len(tickers),
        "tickers": tickers,
    }


@mcp.tool
async def list_strategies(market: str = "IN") -> dict:
    """
    List curated screening strategies with their filter bundles and rationale
    for the given market (IN/US: fundamental+technical; metals/crypto:
    technical only). Pass a strategy's filters to screen_stocks/run_pipeline.
    """
    from pipeline_core.strategies import list_strategies as _list

    return {"market": market, "strategies": _list(market)}


@mcp.tool
async def health() -> dict:
    """Server/config health: LLM mode, defaults, storage locations, and per-data-provider
    health (fallback-chain sources — call counts, success rate, last error)."""
    from pipeline_core.services.providers import provider_health

    return {
        "app": "StockSense MCP",
        "llm_mode": llm_service.llm_mode(),
        "llm_provider": settings.llm_provider,
        "default_index": settings.default_index,
        "reports_dir": str(REPORTS_DIR),
        "data_providers": provider_health(),
    }


# ── Backtesting (Strategy Lab) ─────────────────────────────────────


@mcp.tool
async def list_backtest_strategies(market: str = "IN") -> dict:
    """
    List the trading-strategy library used by the backtester — 35 long-only,
    price/volume-driven strategies across Trend, Momentum, Mean Reversion,
    Breakout, Volume, and Contrarian families (e.g. Golden Cross, Supertrend
    Flip, ADX Trend Rider, RSI Oversold, Connors RSI(2), 52-Week High Breakout,
    Volatility Contraction). Returns id, name, category, and description for
    each — plus any user-promoted discovered strategies for the market (tagged
    discovered:true). Available with no prior run; pass an id to get_backtest_strategy.
    """
    from pipeline_core.backtest import runner

    strategies = runner.strategy_library(market)
    return {"count": len(strategies), "strategies": strategies}


@mcp.tool
async def run_backtest(
    index: Optional[str] = None,
    years: int = 10,
    cost_roundtrip_bps: float = 25.0,
    strategy_ids: Optional[list[str]] = None,
    market: str = "IN",
) -> dict:
    """
    Start a full-universe backtest of the strategy library as a BACKGROUND job
    (it loads up to 10y of daily history for the universe, so it takes minutes —
    do not wait on it). Poll get_backtest_status until running is false, then
    read get_backtest_leaderboard / get_backtest_signals.

    market: "IN" | "US" | "METALS" | "CRYPTO" — selects the universe & benchmark.
    index: universe name (NIFTY 500, S&P 500, NASDAQ-100, Metals, Top 100…), or
    null for the market's default. years: 2–15 (default 10). cost_roundtrip_bps:
    round-trip cost in basis points. strategy_ids: subset, or null for all.
    """
    from pipeline_core.backtest import runner

    if runner.job_state["running"]:
        return {"status": "already_running", "job": runner.job_state}

    years = max(2, min(int(years), 15))

    async def _job() -> None:
        try:
            await runner.run_full_backtest(
                index=index,
                years=years,
                cost_roundtrip_bps=cost_roundtrip_bps,
                strategy_ids=strategy_ids,
                market=market,
            )
        except Exception as e:  # already logged + recorded in job_state
            logger.error(f"Backtest job failed: {e}")

    _background_tasks[f"backtest-{uuid.uuid4()}"] = asyncio.create_task(_job())
    return {
        "status": "started",
        "market": market,
        "index": index,
        "years": years,
        "strategies": len(strategy_ids) if strategy_ids else "all",
        "note": "Poll get_backtest_status; when running is false, read the leaderboard.",
    }


@mcp.tool
async def get_backtest_status(market: str = "IN") -> dict:
    """
    Current backtest job state (running, stage, progress_pct, error) plus a
    summary of the last completed run for the market (when, index, years, elapsed).
    Call after run_backtest to know when results are ready. Results are stored
    per market — pass the same `market` you ran the backtest with.
    """
    from pipeline_core.backtest import runner

    latest = runner.load_latest(market)
    return {
        "job": runner.job_state,
        "market": market,
        "last_run_at": latest.get("run_at") if latest else None,
        "last_run_index": latest.get("index") if latest else None,
        "last_run_years": latest.get("years") if latest else None,
        "elapsed_seconds": latest.get("elapsed_seconds") if latest else None,
    }


@mcp.tool
async def get_backtest_leaderboard(sort_by: str = "sharpe", market: str = "IN") -> dict:
    """
    Ranked leaderboard of every strategy from the latest backtest for the market,
    with full metrics (win_rate, cagr_pct, max_drawdown_pct, sharpe, sortino,
    calmar, profit_factor, expectancy_pct, alpha_vs_benchmark_pct) and live BUY/
    SELL/HOLDING signal counts.

    market ∈ {IN, US, METALS, CRYPTO}. sort_by ∈ {sharpe, win_rate, cagr,
    max_drawdown, profit_factor, expectancy, calmar, alpha, sortino,
    total_return}. Empty if no backtest has been run for that market yet.
    """
    from pipeline_core.backtest import runner

    board = runner.leaderboard(sort_by, market)
    if board is None:
        return {"strategies": [], "run_at": None, "market": market,
                "note": f"No {market} backtest yet — call run_backtest(market='{market}')."}
    return board


@mcp.tool
async def get_backtest_strategy(strategy_id: str, market: str = "IN") -> dict:
    """
    Full detail for one strategy from the latest backtest (for the market): metrics,
    current BUY / SELL / HOLDING stock lists (tickers with signal dates and prices),
    and best/worst sample trades. This is how you find which stocks are giving a buy
    or sell signal under a given strategy. Get valid ids from
    list_backtest_strategies or get_backtest_leaderboard.
    """
    from pipeline_core.backtest import runner

    detail = runner.strategy_detail(strategy_id, include_curves=False, market=market)
    if detail is not None:
        return detail
    if runner.load_latest(market) is None:
        return {"error": f"No {market} backtest results yet — call run_backtest first.", "strategy_id": strategy_id}
    return {"error": f"Strategy '{strategy_id}' not in latest {market} results.", "strategy_id": strategy_id}


@mcp.tool
async def get_backtest_signals(signal_type: Optional[str] = None, market: str = "IN") -> dict:
    """
    Current trading signals across ALL strategies from the latest backtest (for the
    market): per strategy, the stocks flagged BUY (fresh entry), SELL (just exited),
    and HOLDING (open position). signal_type ∈ {buy, sell, holding} narrows each
    strategy to just that list; omit for all three. Empty if no run yet.
    """
    from pipeline_core.backtest import runner

    summary = runner.signals_summary(signal_type, market)
    if summary is None:
        return {"strategies": [], "run_at": None, "market": market,
                "note": f"No {market} backtest yet — call run_backtest."}
    return summary


@mcp.tool
async def get_trade_ideas(max_per_bucket: int = 10, market: str = "IN") -> dict:
    """
    Cross-strategy trade ideas grouped into SHORT / MEDIUM / LONG term buckets,
    computed from the latest backtest's current BUY signals for the market. Each
    strategy is classed by its holding horizon (short ≤ 15d time stop, medium 16–40d,
    long = trend-following with no fixed time stop); its buys are pooled into that
    bucket and aggregated by stock, so a name flagged by more strategies ranks higher
    (conviction). Each bucket is capped at `max_per_bucket` candidates (default 10, hard max 10)
    and includes which strategies flagged each stock and their average win rate.
    Empty until a backtest has run. **Use this to answer "what should I buy for a
    short / medium / long horizon right now?"** market ∈ {IN, US, METALS, CRYPTO}.

    A top-level `regime` block reports the market's risk-on/off tide (benchmark 200-DMA + a
    vol/macro factor — VIX, DXY, or crypto Fear & Greed). It trades WITH the tide: regime-aligned
    longs rank first (each idea tagged `regime_fit` tailwind/headwind), and in risk-off the book is
    sized down (`regime_size_factor`). A neutral/offline reading leaves the board unchanged.
    """
    from pipeline_core.backtest import runner

    ideas = runner.horizon_ideas(max_per_bucket, market)
    if ideas is None:
        return {"horizons": ["short", "medium", "long"], "buckets": {}, "run_at": None,
                "market": market, "note": f"No {market} backtest yet — call run_backtest."}
    return await runner.enrich_ideas_live(ideas, market)   # add live price / zone / recomputed levels


@mcp.tool
async def refresh_trade_ideas(market: str = "IN") -> dict:
    """
    FAST idea generation: re-scan every strategy's current buy signals on **fresh prices** and
    fold them into the market's Trade Ideas board — WITHOUT the slow full 10y backtest (it reuses
    the last run's metrics). Takes seconds, not minutes, and auto-tracks any new ideas. Use this
    to surface newly-triggered setups between full backtests. Requires a prior backtest for the
    market's strategy metrics. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.refresh_current_signals(market)


@mcp.tool
async def get_trade_basket(market: str = "IN", max_per_bucket: int = 10) -> dict:
    """
    The curated **Today's Basket** — the full 30-idea board (10 × short/medium/long) distilled into ONE
    small, deployable best-of of 3–5 trades a user can actually take. **Use this to answer "just tell me
    what to buy right now."** It is the daily analog of the intraday shortlist:

      • balanced / risk-adjusted score — expected value (p·target − (1−p)·stop) × sample/agreement
        confidence × the idea's existing composite rank;
      • greedily DIVERSIFIED — never two names from the same correlation cluster (cross-horizon), with a
        soft per-sector cap and horizon spread;
      • sized as ONE ¼-Kelly book, so each pick's `alloc_pct` is a real portfolio weight summing to
        ≤100% (regime trims the book and shrinks the basket in risk-off);
      • STICKY day-to-day (hysteresis) so it's holdable, not a daily reshuffle.

    Each item carries `alloc_pct`, `win_prob`, `expected_return_pct`, `basket_score`, `horizon`, entry/
    stop/target, and `carried` (held from a prior day). The `summary` gives the basket's expected return,
    blended win rate, exposure vs cash, and diversification. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    ideas = runner.horizon_ideas(max_per_bucket, market)
    if ideas is None:
        return {"enabled": True, "items": [], "market": market,
                "note": f"No {market} backtest yet — call run_backtest."}
    ideas = await runner.enrich_ideas_live(ideas, market)
    return ideas.get("basket") or {"enabled": True, "items": [], "market": market}


@mcp.tool
async def get_smart_money(market: str = "IN", symbol: str = "") -> dict:
    """
    The **smart-money / coattail** signal for one symbol — who the proven, informed money is and whether
    they're accumulating, normalized across markets. Use this to answer "is the smart money buying X?"

      • US     — SEC 13F holdings of a basket of concentrated managers + famed gurus (QoQ delta);
      • METALS — CFTC Commitments of Traders: are the commercial hedgers net-long their 3y range?;
      • CRYPTO — Hyperliquid top-winning-traders' live net positioning in the coin;
      • IN     — disclosed NSE bulk/block deals (a large buyer stepped in).

    Returns direction (accumulate/distribute/neutral), conviction, actors, and a human `detail`.
    `available: false` when no source covers the symbol. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.services import smart_money

    sig = await asyncio.to_thread(smart_money.get_signal, market, symbol)
    return sig or {"available": False, "market": market, "symbol": symbol}


@mcp.tool
async def get_smart_money_leaders(market: str = "CRYPTO", limit: int = 10) -> dict:
    """
    The **coattail leaders board** — who the smart money is buying *right now*, ranked:

      • CRYPTO — the Hyperliquid top-trader book: coins where the best traders are most net long/short;
      • METALS — the metals whose CFTC commercials sit at a positioning extreme;
      • US     — the strongest 13F quarter-over-quarter accumulators in the tracked panel.

    India bulk deals are per-symbol — use get_smart_money(symbol=...) there. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.services import smart_money

    return await asyncio.to_thread(smart_money.get_leaders, market, limit)


@mcp.tool
async def track_trade_basket(market: str = "IN", max_per_bucket: int = 10) -> dict:
    """
    Commit the current curated basket as the day's anchor and track its members so their outcome can be
    followed (idempotent; tagged `source=basket` in the idea tracker). Also advances the day-to-day
    hysteresis anchor. Returns {added, total, basket summary}. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.snapshot_current_ideas(max_per_bucket, market)


@mcp.tool
async def deploy_trade_basket(market: str = "IN") -> dict:
    """
    Deploy the curated basket as a saved, **NAV-tracked Portfolio** so it's tracked as a UNIT — a NAV
    curve, P&L, rebalancing, and AI review (all via the portfolio layer / Portfolios tab) — instead of
    only as individual tracked ideas. Each pick becomes a position at its basket weight; cash = the
    basket's cash. **Idempotent + cost-basis-preserving**: re-deploying re-syncs the same-named portfolio
    and keeps entry prices on names still held (so the NAV curve survives weekly rebalances). Returns
    {deployed, portfolio_id, snapshot, basket}. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.deploy_basket(market)


@mcp.tool
async def get_hold_basket(market: str = "IN") -> dict:
    """
    Your committed **Hold Basket** — the conviction-held, low-churn counterpart to the daily basket.
    **Use this for "what am I holding and do I need to do anything?"** Returns the live holdings + P&L and
    the (usually empty) **material** rebalance plan: a holding only shows EXIT when it hit its stop/target
    or its thesis broke, and a new name only shows ADD when it's dramatically higher-conviction — so a
    still-valid holding is never churned. If nothing is committed yet, returns a `preview` to commit.
    market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.hold_basket_status(market)


@mcp.tool
async def commit_hold_basket(market: str = "IN") -> dict:
    """
    Commit today's hold basket as your book — a conviction-tilted, staying-power set, persisted (entry
    prices + cost basis) and deployed as a NAV-tracked portfolio. It then **stays put** until you apply a
    material change, so you can hold it without daily churn. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.commit_hold_basket(market)


@mcp.tool
async def apply_hold_basket(market: str = "IN") -> dict:
    """
    Apply the reviewed **material** rebalance to your committed Hold Basket (held − exits + material adds),
    keeping the cost-basis on names you're still holding, and re-deploy. A no-op when nothing material has
    changed. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.apply_hold_rebalance(market)


@mcp.tool
async def run_basket_backtest(market: str = "IN", years: int = 8, rebalance_days: int = 5) -> dict:
    """
    Backtest the basket CONSTRUCTION method — *"would this basket approach have made money?"* A
    held-basket, **weekly-rebalanced** historical simulation with anti-lookahead: the board is rebuilt
    point-in-time at each rebalance, the same diversified/regime-scaled/sticky basket is picked, held to
    the next rebalance, and compounded → an equity curve with **CAGR / Sharpe / max-drawdown / alpha vs
    benchmark**, plus turnover and hit-rate. Validates the construction method (the full-sample
    calibration layer is excluded to avoid lookahead). Runs inline (a minute or two on a warm cache);
    result is also saved. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import basket_backtest as bb

    return await bb.run_basket_backtest(market, years, rebalance_days)


@mcp.tool
async def get_basket_backtest(market: str = "IN") -> dict:
    """
    The last basket-method backtest result for the market — equity curve + CAGR/Sharpe/maxDD/alpha vs
    benchmark + turnover/hit-rate. Empty until `run_basket_backtest` has run. market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import basket_backtest as bb

    return bb.load_result(market) or {"ran": False, "market": market, "note": "No basket backtest yet."}


@mcp.tool
async def get_multibagger_ideas(market: str = "IN", top_n: int = 20) -> dict:
    """
    Multi-bagger candidates for the market — a **fundamentals-first**, long-horizon screen (distinct
    from the technical horizon board): high-growth, high-quality, smaller-cap businesses whose growth
    may not be fully priced in (the Peter Lynch "ten-bagger" idea). Each candidate carries a 0–100
    multi-bagger `score` = a coverage-renormalised weighted mean of six pillars (growth, quality,
    balance-sheet, valuation sanity, size/runway, ownership) with a small bounded technical timing
    overlay, plus `reasons` ("why it could multi-bag"), `concerns`, and the raw `metrics`. Loads the
    last persisted screen instantly (call refresh_multibagger_ideas to recompute). **Use this to answer
    "which stocks could compound many times over / multi-bag from here?"** Equity markets only
    (IN / US); price-only markets return an empty, note-bearing payload.
    """
    from pipeline_core import multibagger

    cached = multibagger.load_latest_multibaggers(market)
    if cached is not None:
        cached["candidates"] = cached.get("candidates", [])[:top_n]
        cached["count"] = len(cached["candidates"])
        return cached
    return await multibagger.screen_multibaggers(market, top_n)


@mcp.tool
async def refresh_multibagger_ideas(market: str = "IN", top_n: int = 20) -> dict:
    """
    Recompute the market's multi-bagger screen from fresh fundamentals and return the ranked result.
    Scores a broad mid/small-cap universe (IN: NIFTY 500; US: S&P 500 + NASDAQ-100) from cached
    company profiles, then deep-dives the top survivors (financial statements → Piotroski / ROCE /
    3-year CAGRs, plus IN promoter holding). Takes longer than get_multibagger_ideas on a cold cache;
    the result is persisted so subsequent reads are instant. Equity markets only (IN / US).
    """
    from pipeline_core import multibagger

    return await multibagger.screen_multibaggers(market, top_n)


@mcp.tool
async def track_multibagger_ideas(market: str = "IN", ticker: str = "") -> dict:
    """
    Start tracking multi-bagger candidate(s) so their **bagger multiple** (current ÷ entry price) can
    be followed over time. Pass a `ticker` to track just that one candidate, or leave it empty to
    track the whole current screen. Idempotent (an already-tracked name is skipped). Requires a prior
    screen (get_/refresh_multibagger_ideas). Returns {added, total}. market ∈ {IN, US}.
    """
    from pipeline_core import multibagger

    return multibagger.track_multibaggers(market, ticker or None)


@mcp.tool
async def get_tracked_multibaggers(market: str = "IN") -> dict:
    """
    Follow-up scoreboard for tracked multi-baggers: every tracked name re-priced to a **bagger
    multiple** (2× = a 'double bagger') + return-since-entry + the highest milestone reached, plus a
    fresh **thesis re-score** (entry score → now, so you see if the fundamental case is strengthening
    or weakening) and a basket scoreboard (avg/best multiple, milestone tally). Unlike the horizon
    idea tracker these are open-ended holds — there's no stop/target close. **Use this to answer "how
    are my tracked multi-baggers doing — how far have they run?"** market ∈ {IN, US}.
    """
    from pipeline_core import multibagger

    return await multibagger.tracked_multibaggers_status(market)


@mcp.tool
async def get_tracked_ideas(market: str = "IN") -> dict:
    """
    Follow-up scoreboard for trade ideas (for the market): every tracked idea (auto-snapshot
    on each backtest run, or via the dashboard) evaluated against the latest price. Returns
    per-idea entry / stop / current price, return %, and status (winning / losing /
    stopped_out / flat), plus a scoreboard tallying winners vs losers and the running win
    rate. Prices are fetched live (cached ~15 min). **Use this to answer "how are our trade
    ideas doing — how many are winning vs losing?"** market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return await runner.tracked_ideas_status(market)


@mcp.tool
async def mark_idea_action(ticker: str, horizon: str, market: str = "IN",
                           action: str = "in", price: float = 0.0) -> dict:
    """
    Record what YOU actually did on a tracked idea — the 'you' side of the discipline scorecard.
    action='in' stamps your entry, action='out' your exit, onto the most-recent open tracked record
    for (ticker, horizon, market). price defaults to the live quote when 0/omitted. Pair with
    get_discipline_scorecard to see how your real entries/exits compared to following the plan.
    horizon ∈ {short, medium, long}; market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core.backtest import runner

    return runner.mark_idea_action(ticker, horizon, market, action, price or None)


@mcp.tool
async def get_discipline_scorecard(market: str = "IN") -> dict:
    """
    'System vs you' for the market: how your ACTUAL entries/exits (paper trades + explicit
    I'm-in/out marks) compared to following each idea's plan to its stop / target / expiry.
    Returns per-trade rows (your exit vs the plan's, with a followed / early_cost / early_saved
    verdict) and a scorecard — compounded 'you' vs 'plan' book returns, the net discipline delta,
    and the cost/saved split of early exits. **Use for "am I sticking to the plan — is bailing
    early helping or hurting?"** market ∈ {IN, US, METALS, CRYPTO}.
    """
    from pipeline_core import adherence

    return await adherence.discipline_scorecard(market)


@mcp.tool
async def analyze_trade_idea(ticker: str, market: str = "IN", horizon: str = "") -> dict:
    """
    AI analyst read of a specific trade idea from the current horizon board: a plain-language
    thesis, a **buy / watch / avoid** verdict, the key risk, what would invalidate it, and a
    **news-sentiment** gauge. The verdict blends how many strategies agree, their backtest win
    rate, reward:risk, and the news read. Advisory only — it reviews, never replaces, the
    quant idea; with no LLM configured it returns a deterministic template (`source: rules`).
    horizon ∈ {short, medium, long} (blank = the highest-conviction bucket the ticker is in).
    market ∈ {IN, US, METALS, CRYPTO}. **Use for "what do you think of the <ticker> idea?"**
    """
    from pipeline_core.backtest import idea_ai, runner

    ideas = runner.horizon_ideas(market=market)
    if ideas is None:
        return {"error": f"No {market} backtest yet — call run_backtest first."}
    match, want = None, (horizon or "").lower().strip()
    for h in ideas["horizons"]:
        for c in ideas["buckets"][h]["candidates"]:
            if c["ticker"].upper() != ticker.upper():
                continue
            if want and h == want:
                match = c
                break
            if not want and (match is None or c["conviction"] > match["conviction"]):
                match = c
        if match is not None and want and match.get("horizon") == want:
            break
    if match is None:
        return {"error": f"'{ticker}' is not a current {market} trade idea"
                         + (f" in the {want} horizon." if want else ".")}
    review = await idea_ai.analyze_idea(match, market)
    return {"ticker": match["ticker"], "horizon": match.get("horizon"), "idea": match, "ai": review}


@mcp.tool
async def trade_ideas_postmortem(market: str = "IN") -> dict:
    """
    AI post-mortem over the market's **realized** trade-idea record: what's working and concrete
    lessons, grounded on the closed-trade tally (win rate, targets vs stops vs time-exits, avg
    return, per-horizon breakdown). Advisory; deterministic template with no LLM configured.
    **Use for "how have our trade ideas actually done and what should we learn?"**
    """
    from pipeline_core.backtest import idea_ai, runner

    status = await runner.tracked_ideas_status(market)
    review = await idea_ai.analyze_track_record(status)
    return {"market": (market or "IN").upper(), "review": review,
            "closed_scoreboard": status.get("closed_scoreboard", {})}


# ── Portfolios ─────────────────────────────────────────────────────


@mcp.tool
async def list_portfolio_themes() -> dict:
    """
    Curated thematic baskets used to tilt a portfolio's universe — AI & IT, green
    energy, EV, defense, fintech/digital, pharma/health, PSU banks, consumption,
    manufacturing, infra/realty. Returns slug, name, description, and ticker count.
    Pass a slug as the `theme` argument to construct_portfolio.
    """
    from pipeline_core.portfolio import themes as pf_themes

    return {"themes": pf_themes.list_themes()}


@mcp.tool
async def list_risk_profiles() -> dict:
    """
    The three risk-appetite presets (conservative / balanced / aggressive) and the
    concrete constraints each maps to — universe tier, max positions, per-name &
    sector caps, cash buffer, fallback stop, and allowed strategy categories — plus
    the available weighting schemes and construction bases. Read this before
    construct_portfolio to see the knobs.
    """
    from pipeline_core.portfolio import risk as pf_risk
    from pipeline_core.portfolio.models import PortfolioBasis, WeightingScheme

    return {
        "risk_profiles": pf_risk.list_risk_profiles(),
        "weighting_schemes": [w.value for w in WeightingScheme],
        "bases": [b.value for b in PortfolioBasis],
    }


@mcp.tool
async def construct_portfolio(
    name: str,
    strategy_ids: list[str],
    basis: str = "blend",
    theme: Optional[str] = None,
    risk: str = "balanced",
    weighting: str = "score",
    max_positions: Optional[int] = None,
    notes: Optional[str] = None,
    save: bool = False,
) -> dict:
    """
    Build a weighted, risk-shaped, optionally theme-tilted PORTFOLIO from one or
    more strategies — and optionally persist it.

    basis: 'fundamental' (rank via the fundamental screening strategies, e.g.
      quality-compounders, value-hunters — see list_strategies), 'systematic' (rank
      via the 35 trading strategies' current BUY signals — needs a backtest first,
      see run_backtest / list_backtest_strategies), or 'blend' (both; names flagged
      by both get a conviction boost).
    strategy_ids: ids from the chosen basis. risk: conservative | balanced |
      aggressive — sets the universe tier, position count, per-name/sector caps,
      cash buffer, and which strategy categories are allowed. theme: a slug from
      list_portfolio_themes, or null for no tilt. weighting: equal | score |
      conviction. save=true persists it (retrieve later with list/get_portfolio).

    Returns the constructed portfolio — positions with target weights, per-name
    stops/targets, sector allocation, and a weighted-from-constituent summary.
    Model portfolio for decision-support research, NOT investment advice.
    """
    from pipeline_core.portfolio import PortfolioError
    from pipeline_core.portfolio import construct_portfolio as _construct
    from pipeline_core.portfolio.models import (
        PortfolioBasis,
        PortfolioSpec,
        RiskAppetite,
        WeightingScheme,
    )

    try:
        spec = PortfolioSpec(
            name=name,
            basis=PortfolioBasis(basis),
            strategy_ids=strategy_ids,
            theme=theme,
            risk=RiskAppetite(risk),
            weighting=WeightingScheme(weighting),
            max_positions=max_positions,
            notes=notes,
        )
    except ValueError as e:
        return {"error": f"Invalid parameter: {e}"}

    try:
        pf = await _construct(spec, screen_fn=_orchestrator.screen_only)
    except PortfolioError as e:
        return {"error": str(e)}

    if save:
        pf = await run_store.save_portfolio(pf)
    return {**pf.model_dump(mode="json"), "saved": save}


@mcp.tool
async def list_portfolios() -> dict:
    """All saved portfolios (newest first), each with its spec and positions."""
    portfolios = await run_store.list_portfolios()
    return {"count": len(portfolios), "portfolios": [p.model_dump(mode="json") for p in portfolios]}


@mcp.tool
async def get_portfolio(id_or_name: str) -> dict:
    """One saved portfolio by id or name (positions, weights, summary, disclaimer)."""
    pf = await run_store.get_portfolio(id_or_name)
    if pf is None:
        return {"error": f"No portfolio '{id_or_name}'"}
    return pf.model_dump(mode="json")


@mcp.tool
async def analyze_portfolio(id_or_name: str) -> dict:
    """
    AI read of a saved portfolio: a plain-language construction/risk review with strengths and
    severity-tagged risk notes (concentration, sector tilt, thin cash, weak backtest edge),
    grounded on the weights, sectors, cash, and weighted backtest stats. Advisory only — it
    never re-weights the basket; with no LLM configured it returns a deterministic template
    (`source: rules`). **Use for "what are the risks in <portfolio>?"**
    """
    from pipeline_core.portfolio import ai as pf_ai

    pf = await run_store.get_portfolio(id_or_name)
    if pf is None:
        return {"error": f"No portfolio '{id_or_name}'"}
    return await pf_ai.review_portfolio(pf.model_dump(mode="json"))


@mcp.tool
async def snapshot_portfolio(id_or_name: str) -> dict:
    """
    Mark a saved portfolio to live prices and record a NAV snapshot: per-position
    return vs entry, a weight-weighted portfolio return, and NAV (1.0 = cost basis).
    Snapshots accumulate into a mark-to-market history for tracking performance over
    time. Answers "how is <portfolio> doing since I built it?".
    """
    from pipeline_core.portfolio.track import snapshot_portfolio as _snap

    pf = await run_store.get_portfolio(id_or_name)
    if pf is None:
        return {"error": f"No portfolio '{id_or_name}'"}
    return await _snap(pf)


@mcp.tool
async def rebalance_portfolio(id_or_name: str) -> dict:
    """
    Reconstruct a saved portfolio's target from its stored recipe and diff it against
    the held positions: BUY (newly selected), EXIT (no longer selected), ADD / TRIM
    (drifted beyond the band), HOLD. Held names are marked against the latest price
    for an unrealized return. Answers "what should I rebalance in <portfolio>?".
    """
    from pipeline_core.portfolio import PortfolioError
    from pipeline_core.portfolio import rebalance_portfolio as _rebalance

    pf = await run_store.get_portfolio(id_or_name)
    if pf is None:
        return {"error": f"No portfolio '{id_or_name}'"}
    try:
        result = await _rebalance(pf, screen_fn=_orchestrator.screen_only)
    except PortfolioError as e:
        return {"error": str(e)}
    return result.model_dump(mode="json")


@mcp.tool
async def delete_portfolio(id_or_name: str) -> dict:
    """Delete a saved portfolio by id or name."""
    return {"deleted": await run_store.delete_portfolio(id_or_name)}


# ── Trading bots ────────────────────────────────────────────────────


@mcp.tool
async def list_bots() -> dict:
    """
    List the available trading bots: the intraday crypto **signal** bots (breakout,
    mean-reversion, ema-trend, vwap-reclaim, and the multi-timeframe-confluence flagship)
    plus the daily momentum **basket** bot. Each entry gives its supported timeframes.
    Use scan_crypto_bot to run a signal bot; run_crypto_momentum_bot for the basket.
    """
    from pipeline_core.bots.base import list_bots as _list

    return {"bots": _list()}


@mcp.tool
async def scan_crypto_bot(bot_id: str, timeframe: str = "15m", universe: str = "Top 50", limit: int = 25) -> dict:
    """
    Run an intraday crypto signal bot across the universe on a chosen timeframe and return
    the coins signaling **long or short right now**, each with side + entry / stop / target /
    reward-risk (R). Every bot is bidirectional (crypto is shortable).

    bot_id ∈ breakout | mean-reversion | ema-trend | vwap-reclaim | mtf-confluence (from
    list_bots). timeframe ∈ 5m | 10m | 15m | 30m | 1h | 4h. universe ∈ "Top 50" | "Top 100".
    The mtf-confluence bot additionally gates on the higher-timeframe trend + BTC regime.
    Answers "what should I trade on the 15-minute right now?".
    """
    from pipeline_core.bots import scan as _scan

    return await _scan.scan(bot_id, timeframe=timeframe, universe=universe, limit=limit)


@mcp.tool
async def crypto_intraday_ideas(
    timeframe: str = "4h", universe: str = "Top 50", limit: int = 20, min_conviction: int = 1,
    strict: bool = True, top_k: int = 5, min_score: float = 0.0, sentiment: bool = True,
) -> dict:
    """
    Intraday crypto **Trade Ideas**: aggregate the signal-bot family on one timeframe into
    ranked **long** and **short** idea books, then curate ONE small **`shortlist`** of the best
    setups overall — fewer, higher-conviction ideas (a trader can't act on 40). Each idea gives
    median entry / stop / target, reward-risk (R), and which bots concur.

    The `shortlist` (≤ `top_k`, long + short mixed) scores each candidate on a transparent
    composite: a sample-size-aware **Wilson** win rate (`conf_win_pct`) + measured edge +
    agreement + **live sentiment** — per-coin news + a market-wide crypto **Fear & Greed** gauge
    (top-level `fear_greed`) — applied as a soft down-rank (`sentiment=False` → neutral). Each
    shortlisted idea carries `selection_score`, `conf_win_pct`, a `sentiment` block, `why_selected`,
    and position sizing (`shortlist_sizing`). `min_score` drops weak ones.

    Quality over quantity when a backtest exists: in each regime (trend/range) only the bots
    that are *competitive there* generate ideas (the measurably-worst are pruned), each idea
    carries a signed `quality` (mean measured expectancy) and an `edge_backed` flag, and the
    books rank by measured quality. Absent a backtest, conviction = raw agreeing-bot count.

    timeframe ∈ 5m | 10m | 15m | 30m | 1h | 4h. universe ∈ "Top 50" | "Top 100".
    min_conviction (1-5) requires that many bots to agree before a coin becomes an idea.
    strict=True hides exploratory (non-edge-backed) ideas — a true "stand aside" book when no
    bot shows positive measured edge. This is the intraday counterpart to the daily Trade Ideas.
    """
    from pipeline_core.bots import ideas as _ideas

    return await _ideas.intraday_ideas(
        timeframe=timeframe, universe=universe, limit=limit, min_conviction=min_conviction,
        strict=strict, top_k=top_k, min_score=min_score, sentiment=sentiment)


@mcp.tool
async def run_intraday_backtest(
    timeframes: str = "15m", universe: str = "Top 15", bars: int = 1500,
) -> dict:
    """
    Backtest the intraday crypto signal bots as a BACKGROUND job — a triple-barrier
    walk-forward replay that measures each bot's real win-rate / expectancy / profit-factor
    (per timeframe, sliced by trend/range regime and long/short). It replays every bot over
    deep history on CLOSED bars, so it takes a few minutes — do not wait on it.

    timeframes: comma-separated (e.g. "15m" or "5m,15m,1h"). universe ∈ {"Top 15","Top 50"}.
    bars: history depth per coin (300–5000). Poll get_intraday_bot_edge until the job is idle,
    then read the edge table — which also re-weights intraday Trade Ideas conviction by measured edge.
    """
    from pipeline_core.bots import backtest as _bt

    if _bt.intraday_job_state["running"]:
        return {"status": "already_running", "job": _bt.intraday_job_state}
    tfs = tuple(t.strip() for t in timeframes.split(",") if t.strip()) or ("15m",)

    async def _job() -> None:
        try:
            await _bt.run_intraday_backtest(timeframes=tfs, universe=universe, bars=bars)
        except Exception as e:
            logger.error(f"Intraday backtest job failed: {e}")

    _background_tasks[f"intraday-bt-{uuid.uuid4()}"] = asyncio.create_task(_job())
    return {"status": "started", "timeframes": list(tfs), "universe": universe, "bars": bars,
            "note": "Poll get_intraday_bot_edge; when job.running is false, read the edge table."}


@mcp.tool
async def discover_strategies(
    market: str = "IN", n_candidates: int = 200, years: int = 10,
    split: float = 0.70, top_k: int = 25, seed: Optional[int] = None,
) -> dict:
    """
    DISCOVER new trading strategies as a BACKGROUND job. Samples candidate strategies from a
    parametric grammar (trend / mean-reversion / breakout rule families × risk overlays), ranks
    them on an IN-SAMPLE slice of history, re-tests the top-K on a HELD-OUT out-of-sample slice,
    and keeps only survivors that clear a gate on BOTH slices (min trades, positive expectancy,
    Sharpe, profit-factor) AND a degradation guard (OOS Sharpe ≥ 0.5 × IS Sharpe) — so in-sample-
    only curve-fits are rejected. Takes a few minutes; do not wait on it.

    Survivors STAGE in a catalog (discovered_<MARKET>.json); nothing enters the live library or
    Trade Ideas until you promote_discovered_strategy. n_candidates 10–2000, split 0.4–0.9,
    top_k = how many in-sample leaders get OOS-validated, seed for reproducibility. Poll
    get_discovery_status; when job.running is false, read list_discovered_strategies.
    """
    from pipeline_core.backtest import discovery as _disc

    if _disc.discovery_job_state["running"]:
        return {"status": "already_running", "job": _disc.discovery_job_state}

    async def _job() -> None:
        try:
            await _disc.run_discovery(market=market, n_candidates=n_candidates, years=years,
                                      split=split, top_k=top_k, seed=seed)
        except Exception as e:
            logger.error(f"Discovery job failed: {e}")

    _background_tasks[f"discovery-{uuid.uuid4()}"] = asyncio.create_task(_job())
    return {"status": "started", "market": market, "n_candidates": n_candidates,
            "note": "Poll get_discovery_status; when job.running is false, read list_discovered_strategies."}


@mcp.tool
async def get_discovery_status(market: str = "IN") -> dict:
    """Strategy-discovery job state + a summary of the last completed run (tried / validated / kept)."""
    from pipeline_core.backtest import discovery as _disc

    return _disc.status(market)


@mcp.tool
async def list_discovered_strategies(market: str = "IN") -> dict:
    """
    The DISCOVERED catalog for a market — machine-generated strategies that survived out-of-sample
    validation. Each carries its rule spec, in-sample AND out-of-sample metrics (Sharpe, expectancy,
    profit-factor, trades, CAGR…), a fitness score, and a `promoted` flag. These are data-mined
    candidates, not proven edges — validate before trusting. Promote one with
    promote_discovered_strategy to fold it into the live library on the next backtest run.
    """
    from pipeline_core.backtest import discovery as _disc

    return _disc.list_discovered(market)


@mcp.tool
async def promote_discovered_strategy(strategy_id: str, market: str = "IN", promoted: bool = True) -> dict:
    """
    Promote (or, with promoted=False, un-promote) a discovered strategy. A promoted strategy joins
    the curated 35 on the NEXT full backtest / Trade-Ideas run for the market and thereafter
    resolves like a built-in (leaderboard, detail, trade ideas). strategy_id is the disc-… id from
    list_discovered_strategies.
    """
    from pipeline_core.backtest import discovery as _disc

    return _disc.promote_discovered(strategy_id, market, promoted)


@mcp.tool
async def get_intraday_bot_edge(timeframe: Optional[str] = None, regime: Optional[str] = None) -> dict:
    """
    The measured edge of the intraday signal bots from the last run_intraday_backtest, plus the
    current job state. Each entry (bot × timeframe) gives trades, win_rate, avg_win/avg_loss,
    expectancy, profit_factor, and target/stop/time exit counts, with trend/range and long/short
    breakdowns. Filter by `timeframe` (e.g. "15m") and/or `regime` ("trend" | "range").
    """
    from pipeline_core.bots import backtest as _bt

    st = _bt.status()
    edge = _bt.load_edge()
    if not edge:
        return {**st, "bots": {}, "note": "No intraday backtest has been run yet."}
    bots = edge.get("bots", {})
    if timeframe:
        bots = {k: v for k, v in bots.items() if k.endswith(f"|{timeframe}")}
    if regime:
        bots = {k: (v.get("by_regime", {}).get(regime, v)) for k, v in bots.items()}
    return {"job": st["job"], "as_of": edge.get("as_of"), "coverage": edge.get("coverage"),
            "params": edge.get("params"), "bots": bots}


@mcp.tool
async def run_crypto_momentum_bot(
    top_n: int = 10,
    universe: str = "Top 50",
    deploy: bool = False,
    ai: bool = False,
    apply_ai_risk: bool = False,
) -> dict:
    """
    Crypto Momentum Bot — a BTC-regime-gated, volatility-targeted momentum basket.
    Returns today's target holdings: only risk-on when Bitcoin is above its 200-day
    MA (else all cash), the top `top_n` coins by 30/90-day momentum from the tradable
    universe, inverse-volatility weighted (capped per name) with a trailing stop.

    universe ∈ {"Top 50", "Top 100"}. `ai=true` attaches an LLM risk-manager review
    (briefing, conviction proceed/caution/reduce, per-name risk flags; a rules-only
    template when no LLM is configured); `apply_ai_risk=true` applies its de-risk overlay
    (down-weights flagged names to cash — never adds exposure). `deploy=true` also saves
    the basket as a NAV-tracked portfolio and snapshots it. Schedule it (cron/routine)
    to run it as a live bot.
    """
    from pipeline_core.bots import ai as bot_ai
    from pipeline_core.bots import crypto_momentum

    if deploy:
        return await crypto_momentum.deploy(top_n=top_n, universe=universe,
                                            ai=ai, apply_ai_risk=apply_ai_risk)
    signal = await crypto_momentum.generate_signal(top_n=top_n, universe=universe)
    if ai or apply_ai_risk:
        signal = await bot_ai.annotate(signal, apply=apply_ai_risk)
    return signal


# ── Resources ──────────────────────────────────────────────────────


@mcp.resource("report://{filename}")
def read_report(filename: str) -> str:
    """Read a generated markdown report by filename (from run results)."""
    from pathlib import Path

    clean = Path(filename).name  # traversal guard
    path = REPORTS_DIR / clean
    if not path.exists() or not clean.endswith(".md"):
        return f"Report '{clean}' not found."
    return path.read_text(encoding="utf-8")


@mcp.resource("screen://{name}")
async def read_screen(name: str) -> str:
    """Read a saved screen preset as JSON."""
    screen = await run_store.get_screen(name)
    if screen is None:
        return f'{{"error": "no saved screen named {name}"}}'
    return screen.model_dump_json(indent=2)


# ── Watchlist, Alerts & Digest ─────────────────────────────────────


@mcp.tool
async def get_alerts(market: str = "IN", unread_only: bool = False, limit: int = 50) -> dict:
    """Recent in-app alerts for a market (tracked-idea closures, watchlist price levels, and fresh
    ideas for watched tickers), newest first, plus the unread count. Alerts are generated on the
    background refresh loop. market ∈ {IN, US, METALS, CRYPTO}."""
    return {"alerts": await run_store.list_alerts(market, unread_only=unread_only, limit=limit),
            "unread": await run_store.unread_count(market)}


@mcp.tool
async def add_to_watchlist(ticker: str, market: str = "IN", note: str = "",
                           level_above: Optional[float] = None,
                           level_below: Optional[float] = None) -> dict:
    """Add a ticker to the watchlist, optionally with price levels that raise an alert when reached
    (level_above / level_below). Watched tickers also alert when a fresh trade idea appears."""
    return await run_store.add_watch({"ticker": ticker, "market": market, "note": note,
                                      "level_above": level_above, "level_below": level_below})


@mcp.tool
async def get_watchlist(market: str = "IN") -> dict:
    """The current watchlist for a market (tickers + any price levels being watched)."""
    return {"items": await run_store.list_watch(market)}


@mcp.tool
async def get_digest(market: str = "IN", hours: int = 24) -> dict:
    """Morning brief for a market: what changed in the last `hours` — tracked ideas closed (with a
    win count), alert counts by type, and the open-idea count. **Use for "what happened overnight /
    what should I look at today?"** market ∈ {IN, US, METALS, CRYPTO}."""
    return await alerts.build_digest(market, hours=hours)


@mcp.tool
async def get_calibration(market: str = "IN") -> dict:
    """Win-rate calibration for a market from the realized trade ledger — an isotonic advertised→
    realized reliability curve (Brier score before/after) plus a realized-win-rate table by
    conviction. **Use to answer "are our idea win-rates trustworthy, and does higher conviction
    really win more?"** Too few closed trades → active:false. market ∈ {IN, US, METALS, CRYPTO}."""
    from pipeline_core import calibration

    return calibration.build_from_ledger(market)


@mcp.tool
async def get_fusion_model(market: str = "IN") -> dict:
    """Learned idea-win fusion model for a market — does a logistic model over conviction +
    advertised win-rate + Sharpe beat the base rate out-of-sample (walk-forward validated)? Returns
    the active flag + log-loss before/after + improvement. Active models feed position sizing ahead
    of the single-feature calibration. market ∈ {IN, US, METALS, CRYPTO}."""
    from pipeline_core import fusion

    return fusion.public_view(fusion.build_idea_model(market))


@mcp.tool
async def paper_account(market: str = "IN") -> dict:
    """The paper-trading account for a market, marked to live quotes: equity, realized/unrealized
    P&L, open positions, closed trades, win record, and P&L attribution by source. Auto-closes any
    position whose stop/target the live price has touched. market ∈ {IN, US, METALS, CRYPTO}."""
    return await paper.mark_account(market)


@mcp.tool
async def paper_open(ticker: str, side: str = "long", market: str = "IN", alloc_pct: float = 10.0,
                     stop: Optional[float] = None, target: Optional[float] = None,
                     source: str = "assistant") -> dict:
    """Open a paper position at the live price, sized as `alloc_pct` % of book equity, with an
    optional stop/target. side ∈ {long, short}. Returns the updated account summary."""
    return await paper.open_idea(market, ticker, side, alloc_pct=alloc_pct, stop=stop, target=target,
                                 source=source)


@mcp.tool
async def paper_close(position_id: str, market: str = "IN") -> dict:
    """Close a paper position (from `paper_account` open_positions[].id) at the live price."""
    return await paper.close_idea(market, position_id)


@mcp.tool
async def paper_reset(market: str = "IN", start_balance: float = 100000.0) -> dict:
    """Reset the market's paper account to a fresh starting balance."""
    return await paper.reset_account(market, start_balance)


@mcp.tool
async def backtest_robustness(strategy_id: str, market: str = "IN", windows: int = 4) -> dict:
    """Walk-forward + Monte-Carlo robustness for one strategy: sequential out-of-sample windows (is
    the edge consistent across time, or one lucky regime?) plus a trade-sequence bootstrap (total
    return & max-drawdown confidence bands, probability of a losing run and of ruin). **Use to
    sanity-check a leaderboard strategy before trusting its headline CAGR/Sharpe.** Loads history
    and runs inline. market ∈ {IN, US, METALS, CRYPTO}."""
    from pipeline_core.backtest import runner

    return await runner.run_robustness(strategy_id, market=market, windows=windows)


# ── Entry point ────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="StockSense MCP server")
    parser.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    if args.http:
        logger.info(f"Starting StockSense MCP over HTTP on {args.host}:{args.port}")
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        logger.info("Starting StockSense MCP over stdio")
        mcp.run()


if __name__ == "__main__":
    main()
