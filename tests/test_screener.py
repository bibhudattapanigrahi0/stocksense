"""Screener registry semantics: pass/fail, indeterminate handling, staging."""

import asyncio

import pytest

from pipeline_core.agents.context import StockContext
from pipeline_core.agents.screener import ScreenerAgent, with_derived_metrics
from pipeline_core.models.schemas import ScreenFilters

from conftest import make_price_history


@pytest.fixture
def agent():
    return ScreenerAgent()


@pytest.mark.asyncio
async def test_deep_stage_runs_concurrently_and_correctly():
    """The deep stage fetches survivors in parallel (bounded), not one at a time."""
    agent = ScreenerAgent()
    stocks = [{"ticker": f"S{i}", "name": f"S{i}"} for i in range(6)]

    live = 0
    peak = 0

    async def context_builder(ticker):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)                 # simulate a network fetch
            return StockContext(ticker=ticker, price_history=make_price_history())
        finally:
            live -= 1

    # A technical (deep-stage) filter → every survivor needs the price fetch.
    results = await agent.screen(stocks, ScreenFilters(rsi_max=100.0),
                                 max_candidates=10, context_builder=context_builder)

    assert len(results) == 6                          # rsi ≤ 100 always → all kept
    assert peak >= 2                                  # ran concurrently, not sequentially


@pytest.mark.asyncio
async def test_pe_filter_pass_and_fail(agent, stock_factory):
    stocks = [
        stock_factory(ticker="CHEAP", pe_ratio=10.0),
        stock_factory(ticker="PRICEY", pe_ratio=80.0),
    ]
    filters = ScreenFilters(pe_ratio_max=20.0)
    results = await agent.screen(stocks, filters, max_candidates=10)

    tickers = {r.ticker: r for r in results}
    assert "CHEAP" in tickers
    assert "PRICEY" not in tickers  # fails its only determinate filter → dropped
    assert tickers["CHEAP"].filters_matched == 1
    assert tickers["CHEAP"].filter_match_pct == 100.0


@pytest.mark.asyncio
async def test_missing_data_is_indeterminate_not_failure(agent, stock_factory):
    """A stock missing P/E must not be penalized for a P/E filter."""
    stocks = [
        stock_factory(ticker="NODATA", pe_ratio=None, roe=0.30),
    ]
    filters = ScreenFilters(pe_ratio_max=20.0, roe_min=0.15)
    results = await agent.screen(stocks, filters, max_candidates=10)

    assert len(results) == 1
    r = results[0]
    pe_expl = next(e for e in r.filter_explanations if e.filter_name == "pe_ratio_max")
    assert pe_expl.indeterminate is True
    # Only the ROE filter is determinate → 1/1 = 100%
    assert r.filters_total == 1
    assert r.filter_match_pct == 100.0


@pytest.mark.asyncio
async def test_all_indeterminate_drops_stock(agent, stock_factory):
    stocks = [stock_factory(ticker="GHOST", pe_ratio=None)]
    filters = ScreenFilters(pe_ratio_max=20.0)
    results = await agent.screen(stocks, filters, max_candidates=10)
    assert results == []


@pytest.mark.asyncio
async def test_promoter_pledge_absent_passes(agent, stock_factory):
    stocks = [stock_factory(ticker="NOPLEDGE", promoter_pledge=None)]
    filters = ScreenFilters(promoter_pledge_max=0.05)
    results = await agent.screen(stocks, filters, max_candidates=10)
    assert len(results) == 1
    assert results[0].filter_explanations[0].passed is True


@pytest.mark.asyncio
async def test_promoter_holding_threshold_scale(agent, stock_factory):
    """Filter threshold 0.5 (decimal) vs NSE percent values (×100)."""
    stocks = [
        stock_factory(ticker="HIGHPROM", promoter_holding=62.0),
        stock_factory(ticker="LOWPROM", promoter_holding=30.0),
    ]
    filters = ScreenFilters(promoter_holding_min=0.5)
    results = await agent.screen(stocks, filters, max_candidates=10)
    assert [r.ticker for r in results] == ["HIGHPROM"]


@pytest.mark.asyncio
async def test_debt_to_equity_unit_conversion(agent, stock_factory):
    """yfinance D/E=25 (percent) means 0.25x → passes max 0.5, fails max 0.2."""
    stocks = [stock_factory(debt_to_equity=25.0)]
    ok = await agent.screen(stocks, ScreenFilters(debt_to_equity_max=0.5), max_candidates=10)
    ko = await agent.screen(stocks, ScreenFilters(debt_to_equity_max=0.2), max_candidates=10)
    assert len(ok) == 1
    assert len(ko) == 0


@pytest.mark.asyncio
async def test_sector_substring_match(agent, stock_factory):
    stocks = [stock_factory(sector="Financial Services")]
    results = await agent.screen(stocks, ScreenFilters(sector="financial"), max_candidates=10)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_ranking_orders_by_match_pct(agent, stock_factory):
    stocks = [
        stock_factory(ticker="ONEPASS", pe_ratio=10.0, roe=0.05),
        stock_factory(ticker="BOTHPASS", pe_ratio=10.0, roe=0.30),
    ]
    filters = ScreenFilters(pe_ratio_max=20.0, roe_min=0.15)
    results = await agent.screen(stocks, filters, max_candidates=10)
    assert [r.ticker for r in results] == ["BOTHPASS", "ONEPASS"]
    assert results[0].filter_match_pct == 100.0
    assert results[1].filter_match_pct == 50.0


@pytest.mark.asyncio
async def test_no_filters_returns_first_n(agent, stock_factory):
    stocks = [stock_factory(ticker=f"S{i}") for i in range(5)]
    results = await agent.screen(stocks, ScreenFilters(), max_candidates=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_roce_is_not_roe_copy(agent, stock_factory):
    """Regression: agy reported ROCE as a copy of ROE. Now it's None unless computed."""
    stocks = [stock_factory(roe=0.24)]
    results = await agent.screen(stocks, ScreenFilters(pe_ratio_max=30.0), max_candidates=10)
    assert results[0].roce is None
    assert results[0].roe == 24.0


@pytest.mark.asyncio
async def test_technical_stage_with_context_builder(agent, stock_factory, price_history):
    """Technical filters run only for survivors, sourced via context_builder."""
    from pipeline_core.agents.context import StockContext

    ctx = StockContext(ticker="TECHPASS", info=stock_factory(), price_history=price_history)

    async def builder(ticker: str) -> StockContext:
        return ctx

    stocks = [stock_factory(ticker="TECHPASS", pe_ratio=10.0)]
    filters = ScreenFilters(pe_ratio_max=20.0, price_above_200dma=True)
    results = await agent.screen(stocks, filters, max_candidates=10, context_builder=builder)
    assert len(results) == 1
    # Gently trending-up synthetic series ends well above its 200-DMA
    tech_expl = next(e for e in results[0].filter_explanations if e.filter_name == "price_above_200dma")
    assert tech_expl.passed is True


def test_derived_fcf_yield(stock_factory):
    stock = with_derived_metrics(stock_factory())
    # fcf 2e10 / market_cap 5e11 = 0.04
    assert abs(stock["fcf_yield"] - 0.04) < 1e-9
