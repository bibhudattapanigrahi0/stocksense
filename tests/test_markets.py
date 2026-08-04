"""Market abstraction — ticker normalization, registry, price-only scoring, and
screener gating across asset classes. All network-free."""

import pytest

from pipeline_core import markets
from pipeline_core.agents.context import StockContext
from pipeline_core.agents.orchestrator import OrchestratorAgent
from pipeline_core.agents.screener import ScreenerAgent
from pipeline_core.models.schemas import ScreenFilters

from conftest import make_price_history


# ── Ticker normalization ────────────────────────────────────────────


@pytest.mark.parametrize("symbol,market,expected", [
    ("RELIANCE", "IN", "RELIANCE.NS"),
    ("reliance", "IN", "RELIANCE.NS"),
    ("TCS.NS", "IN", "TCS.NS"),
    ("^NSEI", "IN", "^NSEI"),
    ("AAPL", "US", "AAPL"),
    ("BRK.B", "US", "BRK-B"),
    ("^GSPC", "US", "^GSPC"),
    ("GC=F", "METALS", "GC=F"),
    ("GLD", "METALS", "GLD"),
    ("BTC", "CRYPTO", "BTC-USD"),
    ("eth", "CRYPTO", "ETH-USD"),
    ("SOL-USD", "CRYPTO", "SOL-USD"),
])
def test_normalize_ticker(symbol, market, expected):
    assert markets.normalize_ticker(symbol, market) == expected


@pytest.mark.parametrize("symbol,expected", [
    ("RELIANCE.NS", "RELIANCE"), ("TCS.BO", "TCS"), ("BTC-USD", "BTC"), ("AAPL", "AAPL"),
])
def test_display_ticker(symbol, expected):
    assert markets.display_ticker(symbol) == expected


# ── Registry ────────────────────────────────────────────────────────


def test_get_market_defaults_and_case_insensitive():
    assert markets.get_market("us").key == "US"
    assert markets.get_market(None).key == "IN"
    assert markets.get_market("nonsense").key == "IN"   # unknown → default


def test_registry_integrity():
    for m in markets.MARKETS.values():
        assert m.kind in ("equity", "price_only")
        assert m.benchmark and m.benchmark_label and m.default_universe
        assert m.default_universe in m.universes
        for tier in ("conservative", "balanced", "aggressive"):
            assert tier in m.cap_tiers
        if m.kind == "price_only":
            assert m.price_weights is not None
            assert abs(sum(m.price_weights.values()) - 1.0) < 1e-6
        else:
            assert m.price_weights is None


def test_list_markets_shape():
    pub = {m["key"]: m for m in markets.list_markets()}
    assert set(pub) == {"IN", "US", "METALS", "CRYPTO"}
    assert pub["US"]["has_fundamentals"] is True
    assert pub["METALS"]["has_fundamentals"] is False
    assert pub["IN"]["enrich"] is True
    assert pub["US"]["enrich"] is False


def test_metals_universe_no_network():
    tickers = markets.get_universe_tickers("METALS")
    assert "GC=F" in tickers and "GLD" in tickers


# ── Price-only scoring (metals / crypto) ────────────────────────────


def _ctx(market, df):
    return StockContext(ticker="X", market=market, price_history=df)


def test_momentum_score_ranks_trend():
    up = OrchestratorAgent.momentum_score(_ctx("METALS", make_price_history(days=300, trend=0.003)))
    flat = OrchestratorAgent.momentum_score(_ctx("METALS", make_price_history(days=300, trend=0.0)))
    down = OrchestratorAgent.momentum_score(_ctx("METALS", make_price_history(days=300, trend=-0.003)))
    assert 0 <= down <= 100 and 0 <= flat <= 100 and 0 <= up <= 100
    assert up > flat > down                # uptrend > flat > downtrend
    assert up > 80 and down < 40           # strong trends push toward the extremes


def test_momentum_score_short_history_is_neutral():
    import pandas as pd
    assert OrchestratorAgent.momentum_score(_ctx("METALS", pd.DataFrame())) == 50.0
    assert OrchestratorAgent.momentum_score(_ctx("METALS", make_price_history(days=10))) == 50.0


def test_price_composite_weights_from_market():
    m = markets.get_market("METALS")
    # All sub-scores 100 except risk 0 (risk enters inverted → also 100) ⇒ composite 100.
    assert OrchestratorAgent.price_composite(m, 100, 100, 100, 0) == 100.0
    # All zero, risk 100 (inverted → 0) ⇒ composite 0.
    assert OrchestratorAgent.price_composite(m, 0, 0, 0, 100) == 0.0


# ── Screener gating per market ──────────────────────────────────────


def test_screener_drops_fundamentals_for_price_only():
    filters = ScreenFilters(pe_ratio_max=20, roe_min=0.15, rsi_max=40, macd_bullish=True)
    specs = ScreenerAgent().active_specs(filters, market="METALS")
    fields = {s.field for s in specs}
    assert fields == {"rsi_max", "macd_bullish"}          # only technical survive


def test_screener_drops_nse_stage_off_india():
    filters = ScreenFilters(roe_min=0.15, promoter_holding_min=0.5, delivery_pct_min=0.4)
    us_fields = {s.field for s in ScreenerAgent().active_specs(filters, market="US")}
    assert "roe_min" in us_fields                          # fundamentals stay for US
    assert "promoter_holding_min" not in us_fields         # NSE-only filters dropped
    assert "delivery_pct_min" not in us_fields


def test_screener_keeps_all_for_india():
    filters = ScreenFilters(roe_min=0.15, promoter_holding_min=0.5, rsi_max=40)
    fields = {s.field for s in ScreenerAgent().active_specs(filters, market="IN")}
    assert {"roe_min", "promoter_holding_min", "rsi_max"} <= fields


# ── Price-only deep-dive skips the fundamental agent ────────────────


@pytest.mark.asyncio
async def test_analyze_with_context_price_only_skips_fundamentals():
    from pipeline_core.models.schemas import StockScore

    orch = OrchestratorAgent()

    async def boom(*a, **k):
        raise AssertionError("fundamental agent must not run for price-only markets")

    async def tech(ctx):
        return {"score": 70.0, "signal": None, "rsi": 55.0}

    async def sent(ctx):
        return {"score": 60.0}

    async def risk(ctx):
        return {"score": 40.0, "level": None}

    orch.fundamental.analyze = boom          # type: ignore[assignment]
    orch.technical.analyze = tech            # type: ignore[assignment]
    orch.sentiment.analyze = sent            # type: ignore[assignment]
    orch.risk.analyze = risk                 # type: ignore[assignment]

    ctx = _ctx("METALS", make_price_history(days=300, trend=0.002))
    result = await orch.analyze_with_context(StockScore(ticker="GOLD", name="Gold"), ctx)

    assert result.fundamental_score is None          # no fundamentals for metals
    assert result.momentum_score is not None         # momentum computed instead
    assert 0 <= result.composite_score <= 100
    assert result._analysis["fundamental"].get("skipped")


@pytest.fixture
def anyio_backend():
    return "asyncio"
