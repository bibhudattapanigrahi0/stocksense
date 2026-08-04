"""Portfolio mark-to-market tracking — snapshot NAV math + snapshot persistence.
Network-free (injected fake price_fn, tmp DB)."""

import pytest

from pipeline_core.portfolio.models import Portfolio, PortfolioPosition, PortfolioSpec
from pipeline_core.portfolio.track import snapshot_portfolio
from pipeline_core.services import run_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _portfolio(market="US", cash=0.0):
    return Portfolio(
        id="p1", name="t", cash_pct=cash,
        spec=PortfolioSpec(name="t", market=market, strategy_ids=["x"]),
        positions=[
            PortfolioPosition(ticker="AAA", weight=0.6, entry_price=100.0),
            PortfolioPosition(ticker="BBB", weight=0.4, entry_price=50.0),
        ],
    )


# ── snapshot_portfolio ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_weighted_nav():
    prices = {"AAA": 110.0, "BBB": 45.0}          # +10% and −10%

    async def fake_price(tk):
        return prices.get(tk)

    snap = await snapshot_portfolio(_portfolio(), price_fn=fake_price, persist=False)
    # 0.6·(+10%) + 0.4·(−10%) = +2%
    assert snap["return_pct"] == 2.0 and snap["nav"] == 1.02
    assert snap["priced"] == 2 and snap["positions_total"] == 2
    marks = {m["ticker"]: m for m in snap["positions"]}
    assert marks["AAA"]["return_pct"] == 10.0 and marks["BBB"]["return_pct"] == -10.0


@pytest.mark.asyncio
async def test_snapshot_cash_and_unpriced_are_flat():
    async def fake_price(tk):
        return 120.0 if tk == "AAA" else None      # BBB unpriced

    # 30% cash portfolio; only AAA (weight 0.6) priced → 0.6·(+20%) = +12%.
    pf = _portfolio(cash=0.3)
    snap = await snapshot_portfolio(pf, price_fn=fake_price, persist=False)
    assert snap["return_pct"] == 12.0 and snap["priced"] == 1


# ── run_store snapshot persistence ──────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "test.db")
    await run_store.save_portfolio_snapshot("pf1", {"ts": "2026-07-01T00:00:00Z", "nav": 1.0})
    await run_store.save_portfolio_snapshot("pf1", {"ts": "2026-07-05T00:00:00Z", "nav": 1.08})
    await run_store.save_portfolio_snapshot("pf2", {"ts": "2026-07-05T00:00:00Z", "nav": 0.9})

    hist = await run_store.list_portfolio_snapshots("pf1")
    assert [s["nav"] for s in hist] == [1.0, 1.08]         # oldest-first
    assert (await run_store.latest_portfolio_snapshot("pf1"))["nav"] == 1.08
    assert len(await run_store.list_portfolio_snapshots("pf2")) == 1


@pytest.mark.asyncio
async def test_delete_portfolio_cascades_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "test.db")
    pf = _portfolio()
    await run_store.save_portfolio(pf)
    await run_store.save_portfolio_snapshot(pf.id, {"ts": "2026-07-01T00:00:00Z", "nav": 1.0})
    await run_store.delete_portfolio(pf.id)
    assert await run_store.list_portfolio_snapshots(pf.id) == []
