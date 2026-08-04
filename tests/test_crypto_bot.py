"""Crypto Momentum Bot — regime gate, momentum ranking, inverse-vol sizing, deploy.
Network-free (synthetic price histories, injected snapshot, tmp DB)."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from pipeline_core.bots import crypto_momentum as bot
from pipeline_core.services import run_store


def _series(start, end, n=200, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    base = np.linspace(start, end, n)
    if noise:
        base = base * (1 + rng.normal(0, noise, n))
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"Close": base}, index=idx)


# ── Pure helpers ────────────────────────────────────────────────────


def test_regime_gate():
    assert bot._btc_regime(_series(100, 200))[0] is True     # uptrend → risk-on
    assert bot._btc_regime(_series(200, 100))[0] is False    # downtrend → risk-off


def test_inverse_vol_weights_and_cap():
    w = bot._inverse_vol_weights([0.5, 1.0, 2.0], 0.6)
    assert w[0] > w[1] > w[2]                                # lower vol → bigger weight
    assert abs(sum(w) - 1.0) < 1e-9
    capped = bot._inverse_vol_weights([0.1, 5.0, 5.0], 0.5)  # dominant name capped
    assert max(capped) <= 0.5 + 1e-9 and abs(sum(capped) - 1.0) < 1e-9


# ── generate_signal ─────────────────────────────────────────────────


def _patch_market(monkeypatch, btc, coins):
    monkeypatch.setattr(bot, "get_universe_tickers", lambda market, universe: list(coins))

    async def fake_hist(sym, period="1y", interval="1d", market="CRYPTO"):
        return {"BTC": btc, **coins}[sym]

    monkeypatch.setattr(bot.market_data, "aget_price_history", fake_hist)


def test_signal_risk_off_holds_cash(monkeypatch):
    _patch_market(monkeypatch, btc=_series(200, 100), coins={"AAA": _series(100, 200)})
    sig = asyncio.run(bot.generate_signal())
    assert sig["regime"] == "risk-off" and sig["cash_pct"] == 1.0 and sig["basket"] == []


def test_signal_risk_on_ranks_and_weights(monkeypatch):
    coins = {
        "AAA": _series(100, 220, noise=0.01, seed=1),   # strong momentum
        "BBB": _series(100, 130, noise=0.01, seed=2),   # mild momentum
        "CCC": _series(200, 100, noise=0.01, seed=3),   # negative → excluded
    }
    _patch_market(monkeypatch, btc=_series(100, 200), coins=coins)
    sig = asyncio.run(bot.generate_signal(top_n=10, max_weight=0.8))

    assert sig["regime"] == "risk-on"
    tickers = [b["ticker"] for b in sig["basket"]]
    assert tickers == ["AAA", "BBB"]                         # sorted by momentum, CCC dropped
    assert sig["basket"][0]["momentum_score"] > sig["basket"][1]["momentum_score"]
    assert abs(sum(b["weight"] for b in sig["basket"]) - 1.0) < 1e-6
    assert all(b["stop_price"] < b["price"] for b in sig["basket"])   # stop below entry


# ── deploy ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_materializes_portfolio(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "test.db")
    _patch_market(monkeypatch,
                  btc=_series(100, 200),
                  coins={"AAA": _series(100, 220, noise=0.01), "BBB": _series(100, 140, noise=0.01)})

    # Keep snapshot offline (it would otherwise fetch live prices).
    async def fake_snapshot(pf, **kw):
        return {"ts": "2026-07-27T00:00:00Z", "nav": 1.0, "return_pct": 0.0, "positions": []}

    monkeypatch.setattr("pipeline_core.portfolio.track.snapshot_portfolio", fake_snapshot)

    out = await bot.deploy(top_n=5, name="Bot Test")
    assert out["portfolio_id"]
    saved = await run_store.get_portfolio("Bot Test")
    assert saved is not None and saved.spec.market == "CRYPTO"
    assert [p.ticker for p in saved.positions] == ["AAA", "BBB"]
    assert saved.spec.strategy_ids == [bot.BOT_ID]


@pytest.fixture
def anyio_backend():
    return "asyncio"
