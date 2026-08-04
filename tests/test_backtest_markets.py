"""Per-market backtest storage — Strategy Lab / Trade Ideas / systematic portfolios
each read the selected market's own results. Network-free (pure persist/load)."""

import pytest

from pipeline_core.backtest import runner


def _payload(market, index, bench, sharpe):
    return {
        "run_at": "2026-07-26T00:00:00Z", "market": market, "index": index,
        "benchmark_label": bench, "years": 10, "data_stats": {"loaded": 5, "total": 5},
        "benchmark_equity": [], "disclaimer": "d",
        "strategies": [{
            "id": "golden-cross", "name": "Golden Cross", "category": "Trend",
            "description": "d", "open_positions": 1,
            "signals": {"buy": [], "sell": [], "holding": []},
            "metrics": {"sharpe": sharpe, "win_rate": 55},
        }],
    }


@pytest.fixture
def tmp_results(tmp_path, monkeypatch):
    """Redirect backtest result files to a scratch dir so tests don't touch real data."""
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "_LEGACY_LATEST", tmp_path / "latest.json")
    return tmp_path


def test_persist_and_load_are_per_market(tmp_results):
    runner._persist(_payload("IN", "NIFTY 500", "NIFTY 50", 1.5), "IN")
    runner._persist(_payload("US", "S&P 500", "S&P 500", 2.2), "US")

    assert runner.load_latest("IN")["index"] == "NIFTY 500"
    assert runner.load_latest("US")["index"] == "S&P 500"
    assert runner.load_latest("CRYPTO") is None          # never run → no data


def test_load_latest_heals_non_finite_metrics(tmp_results):
    # A degenerate strategy can produce inf/NaN metrics (e.g. profit_factor with zero
    # losses, calmar with zero drawdown). Python writes these as Infinity/NaN literals,
    # but FastAPI's JSON response rejects them → 500. load_latest must heal them.
    import json
    dirty = _payload("CRYPTO", "Top 100", "Bitcoin", 1.0)
    dirty["strategies"][0]["metrics"].update(
        {"calmar": float("inf"), "sortino": float("-inf"), "cagr_pct": float("nan")}
    )
    runner._latest_file("CRYPTO").write_text(json.dumps(dirty), encoding="utf-8")  # allow_nan default

    latest = runner.load_latest("CRYPTO")
    m = latest["strategies"][0]["metrics"]
    assert m["calmar"] is None and m["sortino"] is None and m["cagr_pct"] is None
    # The healed payload must be strict-JSON serializable (what FastAPI does).
    json.dumps(latest, allow_nan=False)


def test_leaderboard_is_market_scoped(tmp_results):
    runner._persist(_payload("IN", "NIFTY 500", "NIFTY 50", 1.5), "IN")
    runner._persist(_payload("US", "S&P 500", "S&P 500", 2.2), "US")

    inb = runner.leaderboard("sharpe", "IN")
    usb = runner.leaderboard("sharpe", "US")
    assert inb["benchmark_label"] == "NIFTY 50" and inb["strategies"][0]["sharpe"] == 1.5
    assert usb["benchmark_label"] == "S&P 500" and usb["strategies"][0]["sharpe"] == 2.2
    assert runner.leaderboard("sharpe", "METALS") is None


def test_in_falls_back_to_legacy_latest(tmp_results):
    # A pre-multi-market run wrote latest.json (no latest_IN.json yet).
    runner._LEGACY_LATEST.write_text(
        __import__("json").dumps(_payload("IN", "NIFTY 500", "NIFTY 50", 1.1)), encoding="utf-8"
    )
    assert runner.load_latest("IN")["index"] == "NIFTY 500"   # legacy file honored
    assert runner.load_latest("US") is None                   # but only for IN


# ── Idea tracker is market-scoped ───────────────────────────────────


def _tracked_row(ticker, market):
    return {"ticker": ticker, "market": market, "horizon": "short",
            "entry_price": 100.0, "stop_price": 90.0, "conviction": 1,
            "avg_win_rate": 55, "strategies": ["Golden Cross"]}


@pytest.fixture
def tmp_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TRACKED_FILE", tmp_path / "tracked.json")
    # Deterministic "live" price so the scoreboard doesn't hit the network.
    monkeypatch.setattr(runner, "_current_price", lambda tk, market="IN": 110.0)
    return tmp_path


def test_tracker_status_and_clear_are_market_scoped(tmp_tracker):
    import asyncio

    runner._save_tracked([_tracked_row("RELIANCE", "IN"), _tracked_row("TCS", "IN"),
                          _tracked_row("AAPL", "US")])

    us = asyncio.run(runner.tracked_ideas_status("US"))
    assert us["count"] == 1 and us["ideas"][0]["ticker"] == "AAPL"

    inn = asyncio.run(runner.tracked_ideas_status("IN"))
    assert inn["count"] == 2

    assert runner.clear_tracked("US") == {"cleared": 1}       # only US forgotten
    remaining = {t["ticker"] for t in runner._load_tracked()}
    assert remaining == {"RELIANCE", "TCS"}
