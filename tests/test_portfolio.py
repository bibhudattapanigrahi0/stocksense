"""
Portfolio management layer — construction engine, risk/theme config, rebalance,
and persistence. All network-free: the fundamental candidate source is an injected
fake `screen_fn`, the systematic source is a monkeypatched `runner.load_latest`,
and sector/price lookups use injected fakes.
"""

from __future__ import annotations

import pytest

from pipeline_core.models.schemas import StockScore
from pipeline_core.portfolio import construct as C
from pipeline_core.portfolio import risk as pf_risk
from pipeline_core.portfolio import themes as pf_themes
from pipeline_core.portfolio.construct import PortfolioError, construct_portfolio
from pipeline_core.portfolio.models import (
    Portfolio,
    PortfolioBasis,
    PortfolioPosition,
    PortfolioSpec,
    RiskAppetite,
    WeightingScheme,
)
from pipeline_core.portfolio.rebalance import rebalance_portfolio


# ── helpers ────────────────────────────────────────────────────────


def _score(ticker, sector="Utilities", strength=80.0, price=100.0):
    return StockScore(
        ticker=ticker, name=ticker, sector=sector, price=price,
        filter_match_pct=strength, composite_score=strength,
    )


def _fake_screen(stocks):
    """Build an async screen_fn that always returns the given StockScores."""
    async def screen(_request):
        return list(stocks)
    return screen


async def _fake_info(ticker):
    return {"sector": "Utilities", "name": ticker, "price": 100.0}


def _spec(**kw):
    base = dict(
        name="Test PF", basis=PortfolioBasis.FUNDAMENTAL,
        strategy_ids=["quality-compounders"], risk=RiskAppetite.BALANCED,
        weighting=WeightingScheme.SCORE,
    )
    base.update(kw)
    return PortfolioSpec(**base)


# A small fake backtest payload for the systematic path.
FAKE_LATEST = {
    "strategies": [
        {
            "id": "golden-cross", "name": "Golden Cross", "category": "Trend",
            "metrics": {"win_rate": 55.0, "sharpe": 1.2},
            "signals": {"buy": [{"ticker": "NTPC", "price": 350.0}, {"ticker": "TATAPOWER", "price": 400.0}]},
        },
        {
            "id": "dma200-regime", "name": "200DMA Regime", "category": "Trend",
            "metrics": {"win_rate": 50.0, "sharpe": 1.0},
            "signals": {"buy": [{"ticker": "NTPC", "price": 350.0}, {"ticker": "SUZLON", "price": 60.0}]},
        },
    ]
}


# ── themes ─────────────────────────────────────────────────────────


def test_themes_catalog_nonempty_and_wellformed():
    themes = pf_themes.list_themes()
    assert len(themes) >= 8
    slugs = [t["slug"] for t in themes]
    assert "green-energy" in slugs and "ai-it" in slugs
    for t in themes:
        assert t["count"] > 0


def test_theme_universe_tickers_unique_and_uppercase():
    for slug in [t["slug"] for t in pf_themes.list_themes()]:
        tickers = pf_themes.theme_universe(slug)
        assert tickers, f"{slug} is empty"
        assert all(tk == tk.upper() for tk in tickers)
        # get_theme returns a normalized (de-duped) list of the same size as the set
        assert len(pf_themes.get_theme(slug)["tickers"]) == len(tickers)


def test_unknown_theme_is_empty_and_none():
    assert pf_themes.theme_universe("no-such-theme") == set()
    assert pf_themes.theme_universe(None) == set()
    assert pf_themes.get_theme("no-such-theme") is None


def test_sector_matches_theme_widening():
    assert pf_themes.sector_matches_theme("green-energy", "Utilities", "Renewable Power")
    assert not pf_themes.sector_matches_theme("green-energy", "Technology", "Software")


# ── risk ───────────────────────────────────────────────────────────


def test_risk_presets_listed():
    profiles = pf_risk.list_risk_profiles()
    assert len(profiles) == 3
    assert {p["appetite"] for p in profiles} == {"conservative", "balanced", "aggressive"}


def test_conservative_stricter_than_aggressive():
    c = pf_risk.resolve_risk(RiskAppetite.CONSERVATIVE)
    a = pf_risk.resolve_risk(RiskAppetite.AGGRESSIVE)
    assert c.max_positions < a.max_positions
    assert c.per_name_cap > a.per_name_cap        # tighter concentration ceiling
    assert c.cash_pct > a.cash_pct                # more cash buffer
    # aggressive allows strictly more strategy categories
    assert set(c.allowed_categories) < set(a.allowed_categories)
    assert "Contrarian" in a.allowed_categories and "Contrarian" not in c.allowed_categories


def test_risk_overrides_merge():
    r = pf_risk.resolve_risk(RiskAppetite.BALANCED, {"max_positions": 5, "cash_pct": 0.25})
    assert r.max_positions == 5 and r.cash_pct == 0.25
    # untouched fields keep the preset value
    assert r.per_name_cap == pf_risk.RISK_PRESETS[RiskAppetite.BALANCED].per_name_cap
    # appetite itself is not overridable
    r2 = pf_risk.resolve_risk(RiskAppetite.BALANCED, {"appetite": "aggressive"})
    assert r2.appetite == RiskAppetite.BALANCED


# ── construction: fundamental ──────────────────────────────────────


async def test_fundamental_basic_weights_and_cash():
    stocks = [_score(t) for t in ["NTPC", "TATAPOWER", "SUZLON", "JSWENERGY"]]
    pf = await construct_portfolio(
        _spec(strategy_ids=["quality-compounders", "value-hunters"]),
        screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    assert pf.positions
    invested = sum(p.weight for p in pf.positions)
    assert abs(invested + pf.cash_pct - 1.0) < 1e-6
    # each name flagged by both screens → conviction 2
    assert all(p.conviction == 2 for p in pf.positions)
    assert all(p.stop_pct is not None for p in pf.positions)


async def test_theme_filter_drops_off_theme_names():
    stocks = [_score("NTPC"), _score("TATAPOWER"), _score("INFY", sector="Technology")]
    pf = await construct_portfolio(
        _spec(theme="green-energy"), screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    tickers = {p.ticker for p in pf.positions}
    assert "INFY" not in tickers          # not in the green-energy basket
    assert {"NTPC", "TATAPOWER"} <= tickers


async def test_disallowed_category_strategy_is_gated_out():
    stocks = [_score("NTPC"), _score("TATAPOWER")]
    # Momentum is not allowed at conservative risk → 'momentum-breakout' must be skipped.
    pf = await construct_portfolio(
        _spec(strategy_ids=["quality-compounders", "momentum-breakout"], risk=RiskAppetite.CONSERVATIVE),
        screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    all_sources = {s for p in pf.positions for s in p.source_strategies}
    assert "Quality Compounders" in all_sources
    assert "Momentum Breakout" not in all_sources


async def test_max_positions_and_per_name_cap_bind_with_many_names():
    # One dominant score would blow past the per-name cap under score weighting → must be clipped.
    stocks = [_score("BIG", sector="Technology", strength=1000.0)]
    stocks += [_score(f"S{i}", sector=f"Sector{i%6}", strength=50.0) for i in range(15)]
    spec = _spec(strategy_ids=["quality-compounders"], max_positions=10, weighting=WeightingScheme.SCORE)
    pf = await construct_portfolio(spec, screen_fn=_fake_screen(stocks), info_fn=_fake_info)
    risk = pf_risk.resolve_risk(RiskAppetite.BALANCED)
    assert len(pf.positions) == 10
    assert max(p.weight for p in pf.positions) <= risk.per_name_cap + 1e-6


async def test_few_names_relax_caps_and_fully_deploy():
    stocks = [_score(t) for t in ["NTPC", "TATAPOWER", "SUZLON"]]
    pf = await construct_portfolio(_spec(), screen_fn=_fake_screen(stocks), info_fn=_fake_info)
    invested = sum(p.weight for p in pf.positions)
    risk = pf_risk.resolve_risk(RiskAppetite.BALANCED)
    # capital deployed to the cash target despite too few names to honor the per-name cap
    assert abs(invested - (1 - risk.cash_pct)) < 1e-3
    assert any("relaxed" in n or "single-sector" in n for n in pf.summary["notes"])


async def test_empty_strategy_ids_raises():
    with pytest.raises(PortfolioError):
        await construct_portfolio(_spec(strategy_ids=[]), screen_fn=_fake_screen([]), info_fn=_fake_info)


async def test_no_matches_returns_empty_portfolio():
    # Theme filter removes everything → empty basket, not an error.
    stocks = [_score("INFY", sector="Technology")]
    pf = await construct_portfolio(
        _spec(theme="green-energy"), screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    assert pf.positions == []
    assert pf.cash_pct == 1.0


# ── construction: systematic + blend ───────────────────────────────


async def test_systematic_conviction_and_stops(monkeypatch):
    from pipeline_core.backtest import runner
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": FAKE_LATEST)

    pf = await construct_portfolio(
        _spec(basis=PortfolioBasis.SYSTEMATIC, strategy_ids=["golden-cross", "dma200-regime"],
              risk=RiskAppetite.AGGRESSIVE, weighting=WeightingScheme.CONVICTION),
        info_fn=_fake_info,
    )
    by = {p.ticker: p for p in pf.positions}
    assert by["NTPC"].conviction == 2                 # flagged by both strategies
    assert by["TATAPOWER"].conviction == 1
    # conviction weighting → the 2-conviction name carries more weight
    assert by["NTPC"].weight > by["TATAPOWER"].weight
    assert all(p.stop_pct is not None for p in pf.positions)


async def test_systematic_without_backtest_raises(monkeypatch):
    from pipeline_core.backtest import runner
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": None)
    with pytest.raises(PortfolioError):
        await construct_portfolio(
            _spec(basis=PortfolioBasis.SYSTEMATIC, strategy_ids=["golden-cross"]),
            info_fn=_fake_info,
        )


async def test_blend_boosts_names_in_both(monkeypatch):
    from pipeline_core.backtest import runner
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": FAKE_LATEST)

    stocks = [_score("NTPC"), _score("TATAPOWER"), _score("SUZLON")]
    pf = await construct_portfolio(
        _spec(basis=PortfolioBasis.BLEND,
              strategy_ids=["quality-compounders", "golden-cross", "dma200-regime"],
              risk=RiskAppetite.AGGRESSIVE),
        screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    by = {p.ticker: p for p in pf.positions}
    # NTPC is flagged fundamentally AND by both trading strategies → highest conviction & weight
    assert by["NTPC"].conviction >= 3
    assert by["NTPC"].weight == max(p.weight for p in pf.positions)


# ── weighting schemes ──────────────────────────────────────────────


async def test_equal_weighting_is_uniform():
    stocks = [_score("NTPC", strength=90), _score("TATAPOWER", strength=60), _score("SUZLON", strength=30)]
    pf = await construct_portfolio(
        _spec(weighting=WeightingScheme.EQUAL), screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    weights = [p.weight for p in pf.positions]
    assert max(weights) - min(weights) < 1e-6        # all equal


# ── rebalance ──────────────────────────────────────────────────────


async def test_rebalance_actions(monkeypatch):
    stocks = [_score("NTPC", strength=90), _score("TATAPOWER", strength=80), _score("SUZLON", strength=70)]
    target = await construct_portfolio(
        _spec(weighting=WeightingScheme.EQUAL), screen_fn=_fake_screen(stocks), info_fn=_fake_info,
    )
    target.id = "pf-1"
    # Held book: NTPC underweight (ADD), STALE not in target (EXIT), TATAPOWER close (HOLD),
    # SUZLON missing from held (BUY).
    held = target.model_copy(deep=True)
    tgt_w = {p.ticker: p.weight for p in target.positions}
    held.positions = [
        PortfolioPosition(ticker="NTPC", weight=0.05, entry_price=300.0),
        PortfolioPosition(ticker="TATAPOWER", weight=tgt_w["TATAPOWER"], entry_price=380.0),
        PortfolioPosition(ticker="STALE", weight=0.30, entry_price=500.0),
    ]

    result = await rebalance_portfolio(
        held, screen_fn=_fake_screen(stocks), info_fn=_fake_info, mark_prices=False,
    )
    actions = {a.ticker: a.action for a in result.actions}
    assert actions["STALE"] == "EXIT"
    assert actions["SUZLON"] == "BUY"
    assert actions["NTPC"] == "ADD"
    assert actions["TATAPOWER"] == "HOLD"
    assert result.summary["counts"]["EXIT"] == 1


# ── persistence ────────────────────────────────────────────────────


async def test_portfolio_persistence_roundtrip(tmp_path, monkeypatch):
    from pipeline_core.services import run_store
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "pf.db")

    pf = Portfolio(
        name="Green Balanced", spec=_spec(theme="green-energy"),
        positions=[PortfolioPosition(ticker="NTPC", weight=0.5)], cash_pct=0.1,
    )
    saved = await run_store.save_portfolio(pf)
    assert saved.id is not None

    got = await run_store.get_portfolio("Green Balanced")
    assert got is not None and got.spec.theme == "green-energy"
    assert got.spec.basis == PortfolioBasis.FUNDAMENTAL      # enum survives the JSON roundtrip

    assert (await run_store.get_portfolio(saved.id)).id == saved.id
    assert len(await run_store.list_portfolios()) == 1

    assert await run_store.delete_portfolio("Green Balanced") is True
    assert await run_store.get_portfolio("Green Balanced") is None
