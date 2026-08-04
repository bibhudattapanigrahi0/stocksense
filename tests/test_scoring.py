"""Scoring fusion, technical signals, risk levels, sentiment lexicon."""

import pandas as pd
import pytest

from pipeline_core.agents.context import StockContext
from pipeline_core.agents.orchestrator import OrchestratorAgent
from pipeline_core.agents.risk import RiskAssessmentAgent
from pipeline_core.agents.sentiment import SentimentAnalysisAgent
from pipeline_core.agents.technical import TechnicalAnalysisAgent
from pipeline_core.models.schemas import RiskLevel, SignalType


# ── Composite fusion ───────────────────────────────────────────────


def test_composite_weights_default():
    # 80*0.4 + 70*0.3 + 60*0.15 + (100-20)*0.15 = 32+21+9+12 = 74
    assert OrchestratorAgent.composite(80, 70, 60, 20) == 74.0


def test_composite_risk_inverted():
    low_risk = OrchestratorAgent.composite(60, 60, 60, 10)
    high_risk = OrchestratorAgent.composite(60, 60, 60, 90)
    assert low_risk > high_risk


def test_composite_clamped():
    assert OrchestratorAgent.composite(120, 120, 120, 0) <= 100.0
    assert OrchestratorAgent.composite(-10, -10, -10, 120) >= 0.0


# ── Technical agent ────────────────────────────────────────────────


async def test_technical_uptrend_scores_buy(stock_factory, price_history):
    ctx = StockContext(ticker="UP", info=stock_factory(), price_history=price_history)
    result = await TechnicalAnalysisAgent().analyze(ctx)
    assert result["score"] >= 65
    assert result["signal"] == SignalType.BUY
    assert result["above_200dma"] is True
    assert result["triggers"]


async def test_technical_insufficient_data_is_hold(stock_factory):
    ctx = StockContext(ticker="EMPTY", info=stock_factory(), price_history=pd.DataFrame())
    result = await TechnicalAnalysisAgent().analyze(ctx)
    assert result["score"] == 50
    assert result["signal"] == SignalType.HOLD


# ── Risk agent ─────────────────────────────────────────────────────


async def test_risk_low_for_stable_fixture(stock_factory, price_history):
    ctx = StockContext(
        ticker="CALM",
        info=stock_factory(debt_to_equity=20.0, promoter_pledge=None, avg_volume=500000),
        price_history=price_history,  # deterministic, low-vol series
    )
    result = await RiskAssessmentAgent().analyze(ctx)
    assert result["score"] < 40
    assert result["level"] == RiskLevel.LOW


async def test_risk_flags_stack_up(stock_factory, price_history):
    ctx = StockContext(
        ticker="RISKY",
        info=stock_factory(debt_to_equity=250.0, promoter_pledge=40.0, avg_volume=5000),
        price_history=price_history,
    )
    result = await RiskAssessmentAgent().analyze(ctx)
    assert result["score"] >= 65
    assert result["level"] == RiskLevel.HIGH
    assert any("leverage" in f for f in result["risk_flags"])
    assert any("pledge" in f for f in result["risk_flags"])
    assert any("liquidity" in f for f in result["risk_flags"])


# ── Sentiment lexicon fallback ─────────────────────────────────────


async def test_sentiment_lexicon_bullish_headlines(stock_factory):
    ctx = StockContext(
        ticker="HYPE",
        info=stock_factory(),
        news_articles=[
            {"title": "HYPE profit surges to record high, beats estimates", "source": "T", "link": "#"},
            {"title": "HYPE wins major expansion deal, shares rally", "source": "T", "link": "#"},
        ],
    )
    result = await SentimentAnalysisAgent().analyze(ctx)
    assert result["score"] > 60
    assert result["consensus"] == "Bullish"
    assert result["method"] == "lexicon"


async def test_sentiment_lexicon_bearish_headlines(stock_factory):
    ctx = StockContext(
        ticker="DOOM",
        info=stock_factory(),
        news_articles=[
            {"title": "DOOM shares plunge as losses mount, downgrade issued", "source": "T", "link": "#"},
            {"title": "Fraud probe investigation hits DOOM, debt default risk rises", "source": "T", "link": "#"},
        ],
    )
    result = await SentimentAnalysisAgent().analyze(ctx)
    assert result["score"] < 40
    assert result["consensus"] == "Bearish"


async def test_sentiment_no_news_is_neutral(stock_factory):
    ctx = StockContext(ticker="QUIET", info=stock_factory(), news_articles=[])
    agent = SentimentAnalysisAgent()
    # Prevent any network call in the fallback path
    agent._fetch_news = lambda *a, **k: _empty()  # type: ignore[assignment]
    result = await agent.analyze(ctx)
    assert result["score"] == 50.0
    assert result["consensus"] == "Neutral"


async def _empty():
    return []
