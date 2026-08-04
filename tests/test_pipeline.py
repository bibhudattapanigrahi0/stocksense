"""Run-store persistence and orchestrator integration (all I/O mocked)."""

import pytest

from pipeline_core.agents.context import StockContext
from pipeline_core.agents.orchestrator import OrchestratorAgent
from pipeline_core.models.schemas import (
    PipelineRequest,
    PipelineStatus,
    SavedScreen,
    ScreenFilters,
    StockScore,
)
from pipeline_core.services import run_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Run store ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "test.db")

    from pipeline_core.models.schemas import PipelineRun

    run = PipelineRun(id="r1", query="cheap IT stocks", status=PipelineStatus.COMPLETED)
    run.results = [StockScore(ticker="TCS", name="TCS", composite_score=71.0)]
    await run_store.save_run(run)

    loaded = await run_store.get_run("r1")
    assert loaded is not None
    assert loaded.query == "cheap IT stocks"
    assert loaded.results[0].ticker == "TCS"

    runs = await run_store.list_runs()
    assert any(r.id == "r1" for r in runs)

    assert await run_store.delete_run("r1") is True
    assert await run_store.get_run("r1") is None


@pytest.mark.asyncio
async def test_saved_screens_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "test.db")

    screen = SavedScreen(
        name="quality-large-caps",
        description="High ROE, low debt, large cap",
        filters=ScreenFilters(roe_min=0.15, debt_to_equity_max=0.5, market_cap_min_cr=100000),
    )
    saved = await run_store.save_screen(screen)
    assert saved.id is not None

    loaded = await run_store.get_screen("quality-large-caps")
    assert loaded is not None
    assert loaded.filters.roe_min == 0.15

    assert len(await run_store.list_screens()) >= 1
    assert await run_store.delete_screen("quality-large-caps") is True


# ── Orchestrator integration (mocked ingestion) ────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_with_mocked_io(stock_factory, price_history, financials, tmp_path, monkeypatch):
    """
    End-to-end execute() with market data mocked out: intent → ingest →
    screen → analyze → rank → persist. No network, no LLM.
    """
    orch = OrchestratorAgent()

    # Keep report artifacts in a tmp dir, not the real data/reports
    monkeypatch.setattr("pipeline_core.agents.orchestrator.REPORTS_DIR", tmp_path)

    stocks = [
        stock_factory(ticker="ALPHA", pe_ratio=12.0, roe=0.28),
        stock_factory(ticker="BETA", pe_ratio=60.0, roe=0.05),
    ]

    async def fake_universe(index, market="IN"):
        return ["ALPHA", "BETA"]

    async def fake_batch(symbols, progress_callback=None, market="IN"):
        if progress_callback:
            progress_callback(len(symbols), len(symbols))
        return stocks

    async def fake_context(symbol, **kwargs):
        return StockContext(
            ticker=symbol,
            info=next(s for s in stocks if s["ticker"] == symbol),
            price_history=price_history,
            financials=financials,
            # Non-empty so the sentiment agent never touches the network
            news_articles=[{"title": f"{symbol} profit rises on strong growth", "source": "T", "link": "#"}],
        )

    orch.data_ingestion.fetch_universe = fake_universe  # type: ignore[assignment]
    orch.data_ingestion.fetch_batch = fake_batch  # type: ignore[assignment]
    orch.data_ingestion.build_context = fake_context  # type: ignore[assignment]

    progress_events: list[tuple[str, float, int]] = []
    request = PipelineRequest(
        filters=ScreenFilters(pe_ratio_max=20.0, roe_min=0.15),
        include_report=True,
    )
    run = await orch.execute(
        request, progress_cb=lambda s, p, c: progress_events.append((s, p, c))
    )

    assert run.status == PipelineStatus.COMPLETED
    assert run.candidates_found == 1
    assert run.results[0].ticker == "ALPHA"
    # All four sub-scores populated by the analysis stage
    top = run.results[0]
    assert top.fundamental_score is not None
    assert top.technical_score is not None
    assert top.sentiment_score is not None
    assert top.risk_score is not None
    assert 0 <= top.composite_score <= 100
    # Report generated for the top pick
    assert len(run.report_files) == 1
    # Progress flowed
    assert progress_events[-1][1] == 100.0
    # Run persisted
    loaded = await run_store.get_run(run.id)
    assert loaded is not None
    assert loaded.status == PipelineStatus.COMPLETED


@pytest.mark.asyncio
async def test_pipeline_no_matches_completes_cleanly(stock_factory):
    orch = OrchestratorAgent()

    async def fake_universe(index, market="IN"):
        return ["BETA"]

    async def fake_batch(symbols, progress_callback=None, market="IN"):
        return [stock_factory(ticker="BETA", pe_ratio=90.0)]

    orch.data_ingestion.fetch_universe = fake_universe  # type: ignore[assignment]
    orch.data_ingestion.fetch_batch = fake_batch  # type: ignore[assignment]

    run = await orch.execute(
        PipelineRequest(filters=ScreenFilters(pe_ratio_max=10.0), include_report=False)
    )
    assert run.status == PipelineStatus.COMPLETED
    assert run.candidates_found == 0
    assert run.results == []


@pytest.mark.asyncio
async def test_pipeline_ingestion_failure_marks_run_failed():
    orch = OrchestratorAgent()

    async def fake_universe(index, market="IN"):
        return ["X"]

    async def fake_batch(symbols, progress_callback=None, market="IN"):
        return []  # total ingestion failure

    orch.data_ingestion.fetch_universe = fake_universe  # type: ignore[assignment]
    orch.data_ingestion.fetch_batch = fake_batch  # type: ignore[assignment]

    run = await orch.execute(PipelineRequest(filters=ScreenFilters(pe_ratio_max=10.0)))
    assert run.status == PipelineStatus.FAILED
    assert "zero profiles" in run.error
