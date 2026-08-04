"""
Orchestrator Agent — parses intent, schedules pipeline stages, fans out
parallel subagent analysis over shared StockContexts, fuses scores with
configurable weights, persists every run, and emits progress events.

Flow: intent → universe → batched ingest → screen → parallel deep-dive
→ fuse & rank → reports (top-N) → persist.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Callable, Optional

from pipeline_core.agents.context import StockContext
from pipeline_core.agents.data_ingestion import DataIngestionAgent
from pipeline_core.agents.fundamental import FundamentalAnalysisAgent
from pipeline_core.agents.report_generator import ReportGeneratorAgent
from pipeline_core.agents.risk import RiskAssessmentAgent
from pipeline_core.agents.screener import ScreenerAgent
from pipeline_core.agents.sentiment import SentimentAnalysisAgent
from pipeline_core.agents.technical import TechnicalAnalysisAgent
from pipeline_core.config import REPORTS_DIR, settings
from pipeline_core.markets import get_market
from pipeline_core.models.schemas import (
    PipelineRequest,
    PipelineRun,
    PipelineStatus,
    ScreenFilters,
    StockScore,
    utcnow,
)
from pipeline_core.services import llm_service, run_store
from pipeline_core.utils import deep_sanitize

logger = logging.getLogger(__name__)

# Live runs (also persisted to SQLite via run_store for history/restarts)
active_runs: dict[str, PipelineRun] = {}

ProgressCallback = Callable[[str, float, int], None]


class OrchestratorAgent:
    """The manager directing data flow between subagents."""

    def __init__(self):
        self.name = "Orchestrator Agent"
        self.data_ingestion = DataIngestionAgent()
        self.screener = ScreenerAgent()
        self.fundamental = FundamentalAnalysisAgent()
        self.technical = TechnicalAnalysisAgent()
        self.sentiment = SentimentAnalysisAgent()
        self.risk = RiskAssessmentAgent()
        self.report_gen = ReportGeneratorAgent()

    # ── Main pipeline ──────────────────────────────────────────────

    async def execute(
        self,
        request: PipelineRequest,
        progress_cb: Optional[ProgressCallback] = None,
        run_id: Optional[str] = None,
    ) -> PipelineRun:
        """Run the complete pipeline asynchronously. Pass run_id to pre-register the id."""
        run_id = run_id or str(uuid.uuid4())
        run = PipelineRun(
            id=run_id,
            query=request.query,
            filters=request.filters,
            status=PipelineStatus.RUNNING,
            current_stage="Parsing query",
            progress_pct=5.0,
            llm_mode=llm_service.llm_mode(),
        )
        active_runs[run_id] = run
        await run_store.save_run(run)

        def emit(stage: str, pct: float, count: int = 0) -> None:
            run.current_stage = stage
            run.progress_pct = pct
            if progress_cb:
                progress_cb(stage, pct, count)

        try:
            # 1. Intent extraction (LLM structured parse → validated merge)
            if request.query:
                emit("Parsing intent", 5.0, 0)
                parsed_filters = await llm_service.parse_natural_language_query(request.query)
                if parsed_filters:
                    logger.info(f"Parsed NL filters: {parsed_filters}")
                    merged = {
                        **request.filters.model_dump(exclude_unset=True),
                        **parsed_filters,
                    }
                    request.filters = ScreenFilters.model_validate(merged)
                    run.filters = request.filters
                else:
                    logger.info("NL parse unavailable/empty — using explicit filters only")

            # 2. Universe
            market = request.market or settings.default_market
            m = get_market(market)
            emit("Fetching index constituents", 12.0, 0)
            index_name = request.filters.index or m.default_universe
            tickers = await self.data_ingestion.fetch_universe(index_name, market=m.key)

            # 3. Batched ingestion with bounded concurrency
            def on_ingest_progress(done: int, total: int) -> None:
                pct = 15.0 + (done / max(total, 1) * 30.0)
                emit(f"Ingesting profiles ({done}/{total})", pct, done)

            emit("Ingesting stock profiles", 15.0, len(tickers))
            stock_data_list = await self.data_ingestion.fetch_batch(
                tickers, progress_callback=on_ingest_progress, market=m.key
            )
            if not stock_data_list:
                raise RuntimeError(
                    f"Ingestion returned zero profiles for {m.key}/{index_name} — "
                    "market data source unreachable?"
                )

            # 4. Screening (staged: info → NSE/technical/quality on survivors)
            emit("Applying screen filters", 50.0, len(stock_data_list))
            screened = await self.screener.screen(
                stock_data_list,
                request.filters,
                max_candidates=request.max_candidates,
                market=m.key,
            )
            run.candidates_found = len(screened)

            if not screened:
                run.status = PipelineStatus.COMPLETED
                emit("Completed — no stocks matched the filters", 100.0, 0)
                run.completed_at = utcnow()
                await run_store.save_run(run)
                return run

            # 5. Parallel deep-dive analysis of candidates
            emit(f"Deep-diving {len(screened)} candidates", 60.0, len(screened))
            depth = request.analysis_depth.value
            semaphore = asyncio.Semaphore(settings.analysis_concurrency)
            analyzed = 0

            async def analyze_one(candidate: StockScore) -> StockScore:
                nonlocal analyzed
                async with semaphore:
                    try:
                        result = await self._analyze_single_stock(candidate, depth, m.key)
                    except Exception as e:
                        logger.error(f"Analysis failed for {candidate.ticker}: {e}")
                        result = candidate  # keep screening data on failure
                    analyzed += 1
                    pct = 60.0 + (analyzed / len(screened) * 25.0)
                    emit(f"Analyzed {analyzed}/{len(screened)}", pct, analyzed)
                    return result

            final_scores = list(await asyncio.gather(*(analyze_one(c) for c in screened)))

            # 6. Fuse & rank
            final_scores.sort(key=lambda s: s.composite_score or 0.0, reverse=True)
            run.results = final_scores

            # 7. Reports for top-N
            if request.include_report and final_scores:
                top_n = max(1, settings.report_top_n)
                for rank, stock in enumerate(final_scores[:top_n], start=1):
                    emit(f"Drafting report {rank}/{top_n}: {stock.ticker}", 88.0 + rank, len(final_scores))
                    report_path = await self._generate_report(stock, depth, run_id, m.key)
                    if report_path:
                        run.report_files.append(report_path)

            # Done
            run.status = PipelineStatus.COMPLETED
            emit("Pipeline finished", 100.0, len(final_scores))
            run.completed_at = utcnow()

        except Exception as e:
            logger.error(f"Orchestrator pipeline failed: {e}", exc_info=True)
            run.status = PipelineStatus.FAILED
            run.error = str(e)
            emit(f"Pipeline error: {e}", 100.0, 0)
            run.completed_at = utcnow()

        await run_store.save_run(run)
        return run

    # ── Convenience entry points (shared by REST + MCP surfaces) ───

    async def screen_only(self, request: PipelineRequest) -> list[StockScore]:
        """
        Lightweight path: intent → universe → ingest → screen, without the
        deep-dive analysis stage. Used by the `screen_stocks` tool/endpoint.
        """
        market = request.market or settings.default_market
        m = get_market(market)
        if request.query:
            parsed_filters = await llm_service.parse_natural_language_query(request.query, market=m.key)
            if parsed_filters:
                merged = {**request.filters.model_dump(exclude_unset=True), **parsed_filters}
                request.filters = ScreenFilters.model_validate(merged)

        index_name = request.filters.index or m.default_universe
        tickers = await self.data_ingestion.fetch_universe(index_name, market=m.key)
        stock_data_list = await self.data_ingestion.fetch_batch(tickers, market=m.key)
        if not stock_data_list:
            raise RuntimeError(f"Ingestion returned zero profiles for {m.key}/{index_name}")
        return await self.screener.screen(
            stock_data_list, request.filters, max_candidates=request.max_candidates, market=m.key
        )

    async def analyze_ticker(self, ticker: str, depth: str = "standard", market: str = "IN") -> dict:
        """
        Full deep dive for a single ticker (four agents for equities; a
        technical/momentum composite for price-only markets). Returns a
        structured dict with sub-scores, composite, and each agent's evidence.
        """
        ctx = await self.data_ingestion.build_context(ticker, include_news=True, market=market)
        if "error" in ctx.info and not ctx.has_prices():
            return {"ticker": ticker, "error": ctx.info["error"]}

        candidate = StockScore(ticker=ctx.ticker, name=ctx.name)
        candidate = await self.analyze_with_context(candidate, ctx, depth)

        return deep_sanitize({
            "ticker": candidate.ticker,
            "name": candidate.name,
            "market": ctx.market,
            "composite_score": candidate.composite_score,
            "fundamental_score": candidate.fundamental_score,
            "technical_score": candidate.technical_score,
            "sentiment_score": candidate.sentiment_score,
            "risk_score": candidate.risk_score,
            "momentum_score": candidate.momentum_score,
            "technical_signal": candidate.technical_signal,
            "risk_level": candidate.risk_level,
            "roce": candidate.roce,
            "rsi": candidate.rsi,
            "llm_mode": llm_service.llm_mode(),
            "evidence": {
                "fundamental": candidate._analysis.get("fundamental", {}),
                "technical": candidate._analysis.get("technical", {}),
                "sentiment": {
                    k: v for k, v in candidate._analysis.get("sentiment", {}).items()
                    if k != "articles"
                },
                "risk": candidate._analysis.get("risk", {}),
            },
        })

    # ── Per-stock analysis ─────────────────────────────────────────

    async def _analyze_single_stock(self, candidate: StockScore, depth: str, market: str = "IN") -> StockScore:
        """Build one StockContext and run the analysis agents against it."""
        ctx = await self.data_ingestion.build_context(
            candidate.ticker,
            include_news=True,
            market=market,
        )
        return await self.analyze_with_context(candidate, ctx, depth)

    async def analyze_with_context(
        self,
        candidate: StockScore,
        ctx: StockContext,
        depth: str = "standard",
    ) -> StockScore:
        """
        Run the analysis agents concurrently on a shared context and fuse into a
        composite score. Equity markets use the fundamental-weighted composite;
        price-only markets (metals, crypto) skip the fundamental agent and use a
        technical/momentum composite.
        """
        m = get_market(ctx.market)

        if m.has_fundamentals:
            f_res, t_res, s_res, r_res = await asyncio.gather(
                self.fundamental.analyze(ctx, depth),
                self.technical.analyze(ctx),
                self.sentiment.analyze(ctx),
                self.risk.analyze(ctx),
                return_exceptions=True,
            )
            f_res = f_res if isinstance(f_res, dict) else {"score": 50.0, "error": str(f_res)}
        else:
            # Price-only: no fundamentals exist for metals/crypto.
            t_res, s_res, r_res = await asyncio.gather(
                self.technical.analyze(ctx),
                self.sentiment.analyze(ctx),
                self.risk.analyze(ctx),
                return_exceptions=True,
            )
            f_res = {"score": None, "skipped": "no fundamentals for this market"}

        t_res = t_res if isinstance(t_res, dict) else {"score": 50.0, "error": str(t_res)}
        s_res = s_res if isinstance(s_res, dict) else {"score": 50.0, "error": str(s_res)}
        r_res = r_res if isinstance(r_res, dict) else {"score": 50.0, "error": str(r_res)}

        candidate.fundamental_score = f_res.get("score")
        candidate.technical_score = t_res.get("score", 50.0)
        candidate.sentiment_score = s_res.get("score", 50.0)
        candidate.risk_score = r_res.get("score", 50.0)

        if m.has_fundamentals:
            candidate.composite_score = self.composite(
                candidate.fundamental_score or 50.0,
                candidate.technical_score,
                candidate.sentiment_score,
                candidate.risk_score,
            )
        else:
            candidate.momentum_score = self.momentum_score(ctx)
            candidate.composite_score = self.price_composite(
                m,
                candidate.technical_score,
                candidate.momentum_score,
                candidate.sentiment_score,
                candidate.risk_score,
            )

        candidate.technical_signal = t_res.get("signal")
        candidate.risk_level = r_res.get("level")
        if candidate.roce is None and f_res.get("roce") is not None:
            candidate.roce = round(f_res["roce"] * 100, 2)
        if candidate.rsi is None:
            candidate.rsi = t_res.get("rsi")

        # Stash evidence for the report stage
        candidate._analysis = {  # noqa: SLF001 — internal evidence carrier
            "fundamental": f_res,
            "technical": t_res,
            "sentiment": s_res,
            "risk": r_res,
        }
        return candidate

    @staticmethod
    def composite(fundamental: float, technical: float, sentiment: float, risk: float) -> float:
        """
        Weighted composite (0–100). Risk enters inverted: a high risk score
        drags the composite down. Weights come from settings.
        """
        settings.validate_weights()
        risk_contribution = 100.0 - risk
        value = (
            fundamental * settings.weight_fundamental
            + technical * settings.weight_technical
            + sentiment * settings.weight_sentiment
            + risk_contribution * settings.weight_risk
        )
        return round(max(0.0, min(100.0, value)), 1)

    @staticmethod
    def price_composite(market, technical: float, momentum: float, sentiment: float, risk: float) -> float:
        """
        Composite for price-only markets (metals, crypto) — no fundamentals.
        Weights come from the market's `price_weights`; risk enters inverted.
        """
        w = market.price_weights or {"technical": 0.45, "momentum": 0.30, "risk": 0.15, "sentiment": 0.10}
        value = (
            technical * w.get("technical", 0.0)
            + momentum * w.get("momentum", 0.0)
            + sentiment * w.get("sentiment", 0.0)
            + (100.0 - risk) * w.get("risk", 0.0)
        )
        return round(max(0.0, min(100.0, value)), 1)

    @staticmethod
    def momentum_score(ctx: StockContext) -> float:
        """
        Trend/momentum score (0–100) from price history alone — used by
        price-only markets. Blends trailing 3/6/12-month returns and distance
        from the 52-week high. Neutral 50 when history is too short.
        """
        df = ctx.price_history
        if df is None or df.empty or "Close" not in df or len(df) < 40:
            return 50.0
        close = df["Close"].astype(float)
        last = float(close.iloc[-1])
        if last <= 0:
            return 50.0

        def trailing_return(days: int) -> Optional[float]:
            if len(close) <= days:
                return None
            past = float(close.iloc[-days - 1])
            return (last - past) / past if past > 0 else None

        # Map each trailing return to 0–100 (a +/-50% move saturates near 100/0).
        parts: list[float] = []
        for days in (63, 126, 252):  # ~3m, 6m, 12m of trading days
            r = trailing_return(days)
            if r is not None:
                parts.append(max(0.0, min(100.0, 50.0 + r * 100.0)))
        # Distance from 52-week high: at the high → 100, 50% below → ~50.
        window = close.tail(252)
        high = float(window.max())
        if high > 0:
            parts.append(max(0.0, min(100.0, 100.0 - (high - last) / high * 100.0)))

        return round(sum(parts) / len(parts), 1) if parts else 50.0

    # ── Reports ────────────────────────────────────────────────────

    async def _generate_report(self, stock: StockScore, depth: str, run_id: str, market: str = "IN") -> Optional[str]:
        """Markdown report for one ranked stock; returns the file path."""
        try:
            analysis = getattr(stock, "_analysis", None)
            if analysis is None:
                ctx = await self.data_ingestion.build_context(stock.ticker, include_news=True, market=market)
                stock = await self.analyze_with_context(stock, ctx, depth)
                analysis = stock._analysis  # noqa: SLF001

            report_md = await self.report_gen.generate_markdown_report(
                symbol=stock.ticker,
                name=stock.name,
                fundamental=analysis["fundamental"],
                technical=analysis["technical"],
                sentiment=analysis["sentiment"],
                risk=analysis["risk"],
                composite_score=stock.composite_score or 50.0,
            )

            report_file = REPORTS_DIR / f"{stock.ticker}_{run_id}.md"
            report_file.write_text(report_md, encoding="utf-8")
            logger.info(f"Saved report: {report_file}")
            return report_file.name
        except Exception as e:
            logger.error(f"Report generation failed for {stock.ticker}: {e}")
            return None
