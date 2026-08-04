"""Fast signals-only idea refresh — buy detection, latest.json merge (buys replaced,
metrics/sell/holding preserved), and the market-hours gate. Network-free (stubs)."""

import asyncio

import pandas as pd

from pipeline_core.backtest import runner
from pipeline_core.backtest.strategies import STRATEGIES


class _FakeStrat:
    id = "x"; name = "X"; min_bars = 3

    def entry(self, df):
        s = pd.Series(False, index=df.index)
        s.iloc[-1] = True                       # fire on the latest bar
        return s


def _df(closes, start="2026-06-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=idx)


# ── scan_fresh_buys ─────────────────────────────────────────────────


def test_scan_fresh_buys_flags_recent_entry():
    df = _df([100 + i for i in range(8)])
    buys = runner.scan_fresh_buys(_FakeStrat(), {"AAA": df, "BBB": df}, lookback_days=3)
    assert {b["ticker"] for b in buys} == {"AAA", "BBB"}
    assert buys[0]["price"] == 107                    # round_price(last close)


def test_scan_fresh_buys_none_when_quiet():
    class Quiet(_FakeStrat):
        def entry(self, df):
            return pd.Series(False, index=df.index)

    assert runner.scan_fresh_buys(Quiet(), {"AAA": _df([1, 2, 3, 4, 5])}) == []


# ── refresh_current_signals: merge semantics ────────────────────────


def test_refresh_replaces_buys_and_preserves_rest(monkeypatch):
    sid = STRATEGIES[0].id
    latest = {
        "market": "IN", "index": "NIFTY 50", "years": 10, "run_at": "2026-07-01T00:00:00Z",
        "benchmark_label": "NIFTY 50", "disclaimer": "d",
        "strategies": [{
            "id": sid, "name": "S", "category": "Trend",
            "metrics": {"win_rate": 55, "sharpe": 1.2},
            "signals": {"buy": [{"ticker": "OLD", "signal_date": "2026-06-01", "price": 10}],
                        "sell": [{"ticker": "SX"}], "holding": [{"ticker": "HX"}], "as_of": "2026-06-01"},
        }],
    }
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": latest)

    async def fake_universe(index, market, years):
        return {"AAA": _df([1, 2, 3, 4, 5], start="2026-07-01")}

    monkeypatch.setattr(runner, "_load_recent_universe", fake_universe)
    monkeypatch.setattr(runner, "scan_fresh_buys",
                        lambda s, d, lb=3: [{"ticker": "AAA", "signal_date": "2026-07-07", "price": 5}])
    captured = {}
    monkeypatch.setattr(runner, "_persist", lambda payload, market: captured.update(p=payload))
    monkeypatch.setattr(runner, "snapshot_current_ideas", lambda market="IN": {"added": 2})

    res = asyncio.run(runner.refresh_current_signals("IN"))
    assert res["updated"] and res["total_buys"] == 1 and res["added_tracked"] == 2
    strat = captured["p"]["strategies"][0]
    assert [b["ticker"] for b in strat["signals"]["buy"]] == ["AAA"]     # buys refreshed
    assert strat["signals"]["sell"] == [{"ticker": "SX"}]               # preserved
    assert strat["signals"]["holding"] == [{"ticker": "HX"}]            # preserved
    assert strat["metrics"] == {"win_rate": 55, "sharpe": 1.2}          # metrics untouched
    assert captured["p"]["signals_refreshed_at"]                        # freshness stamp added


def test_refresh_no_backtest_yet(monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": None)
    res = asyncio.run(runner.refresh_current_signals("IN"))
    assert res["updated"] is False and "backtest" in res["note"].lower()


def test_load_recent_universe_fetches_and_filters(monkeypatch):
    # Exercises the REAL loader (concurrency uses settings.ingestion_concurrency).
    monkeypatch.setattr(runner, "get_index_tickers", lambda index, market: ["AAA", "BBB", "CC"])
    idx = pd.date_range("2026-05-01", periods=80, freq="B")

    async def fake_hist(tk, period, interval, market):
        return pd.DataFrame() if tk == "CC" else pd.DataFrame({"Close": range(80)}, index=idx)

    monkeypatch.setattr(runner, "aget_price_history", fake_hist)
    out = asyncio.run(runner._load_recent_universe("NIFTY 50", "IN", 2))
    assert sorted(out) == ["AAA", "BBB"]                 # CC dropped (empty / < 60 bars)


# ── market-hours gate ───────────────────────────────────────────────


def test_market_active_crypto_and_unknown():
    assert runner._market_active("CRYPTO") is True
    assert runner._market_active("WHATEVER") is True     # unknown class → don't block
