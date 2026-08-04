"""Fundamental quality metrics: real ROCE, Piotroski, coverage, CAGRs."""

from pipeline_core.agents.fundamental import (
    FundamentalAnalysisAgent,
    compute_interest_coverage,
    compute_piotroski,
    compute_quality_metrics,
    compute_roce,
)
from pipeline_core.agents.context import StockContext


def test_roce_computed_from_statements(financials):
    # EBIT 2600 / (15000 - 3000) = 0.2167
    roce = compute_roce(financials)
    assert roce is not None
    assert abs(roce - 2600 / 12000) < 1e-3


def test_roce_none_when_statements_missing():
    assert compute_roce({}) is None
    assert compute_roce({"income_stmt": {}, "balance_sheet": {}}) is None


def test_interest_coverage(financials):
    coverage = compute_interest_coverage(financials)
    assert abs(coverage - 2600 / 180) < 1e-6


def test_interest_coverage_zero_interest():
    fin = {"income_stmt": {"2025": {"EBIT": 100.0, "Interest Expense": 0.0}}}
    assert compute_interest_coverage(fin) is None


def test_piotroski_healthy_company_scores_high(financials):
    score, details = compute_piotroski(financials)
    # Fixture: positive NI, positive OCF, improving ROA, OCF>NI, falling leverage,
    # improving current ratio, no dilution, improving GM, improving asset turnover
    assert score == 9
    assert len(details) == 9


def test_piotroski_none_without_statements():
    score, details = compute_piotroski({})
    assert score is None
    assert details == []


def test_quality_metrics_cagr(financials):
    metrics = compute_quality_metrics(financials)
    # EPS 34 → 42 over 1 year = ~23.5%
    assert abs(metrics["eps_growth_3y"] - (42 / 34 - 1)) < 1e-6
    # Revenue 10000 → 12000 = 20%
    assert abs(metrics["sales_growth_3y"] - 0.20) < 1e-6
    assert metrics["piotroski_score"] == 9


def test_cagr_guard_against_sign_crossing(financials):
    # Flip old EPS negative: CAGR must be None, not a complex/root error
    for col in financials["income_stmt"].values():
        pass
    financials["income_stmt"][sorted(financials["income_stmt"])[0]]["Diluted EPS"] = -5.0
    metrics = compute_quality_metrics(financials)
    assert metrics["eps_growth_3y"] is None


async def test_fundamental_agent_end_to_end(stock_factory, price_history, financials):
    ctx = StockContext(
        ticker="TESTCORP",
        info=stock_factory(),
        price_history=price_history,
        financials=financials,
    )
    agent = FundamentalAnalysisAgent()
    result = await agent.analyze(ctx, depth="quick")

    assert result["score"] > 70  # healthy fixture should score well
    assert result["piotroski_score"] == 9
    assert result["roce"] is not None
    assert result["strengths"]
    assert result["valuation_status"] in ("Undervalued", "Fairly Valued", "Overvalued")
    # quick depth → no moat assessment
    assert "moat_assessment" not in result


async def test_fundamental_agent_moat_fallback_without_llm(stock_factory, financials):
    ctx = StockContext(ticker="TESTCORP", info=stock_factory(), financials=financials)
    agent = FundamentalAnalysisAgent()
    result = await agent.analyze(ctx, depth="standard")
    # No LLM configured in tests → honest fallback, not an error string
    assert "requires an LLM provider" in result["moat_assessment"]
