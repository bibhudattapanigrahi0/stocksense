"""Idea tracker lifecycle — trades close on stop / target / expiry → past trades,
with a realized track record. Network-free (synthetic OHLC)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pipeline_core.backtest import runner


def _hist(closes, start="2026-06-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c}, index=idx)


def _idea(**over):
    base = dict(ticker="BTC", market="CRYPTO", horizon="medium", entry_price=100.0,
                stop_price=95.0, target_price=110.0, signal_date="2026-06-01",
                tracked_at="2026-06-01T00:00:00+00:00")
    base.update(over)
    return base


# ── _evaluate_idea_path ─────────────────────────────────────────────


def test_path_hits_target():
    out = runner._evaluate_idea_path(_idea(), _hist([100, 102, 105, 111, 108]))
    assert out["closed"] and out["exit_reason"] == "target"
    assert out["exit_price"] == 110.0 and out["realized_return_pct"] == 10.0


def test_path_hits_stop():
    out = runner._evaluate_idea_path(_idea(), _hist([100, 98, 95, 97]))
    assert out["closed"] and out["exit_reason"] == "stop"
    assert out["exit_price"] == 95.0 and out["realized_return_pct"] == -5.0


def test_path_expires_on_time():
    out = runner._evaluate_idea_path(_idea(horizon="medium"), _hist([100] + [101] * 45))
    assert out["closed"] and out["exit_reason"] == "time" and out["bars_held"] == 40


def test_path_still_open():
    assert runner._evaluate_idea_path(_idea(), _hist([100, 101, 102, 103])) is None


def test_legacy_idea_without_target_still_closes_on_target():
    # Legacy ideas were snapshotted with no target_price → they could only ever close
    # on stop/time, so a completed winner lingered forever in the open list.
    legacy = {"ticker": "X", "market": None, "horizon": "short",
              "entry_price": 100.0, "stop_price": 94.0, "signal_date": "2026-06-01"}
    assert runner._effective_target(legacy) == 110.0          # short default +10%
    out = runner._evaluate_idea_path(legacy, _hist([100, 104, 108, 112, 109]))
    assert out["exit_reason"] == "target" and out["realized_return_pct"] == 10.0
    # A winner that hasn't reached the target stays open.
    assert runner._evaluate_idea_path(legacy, _hist([100, 101, 102, 103])) is None


def test_subdollar_precision_not_collapsed():
    # Regression: round(price, 2) collapsed a $0.0123 coin's stop/target to 0.01,
    # closing it instantly at 0%. round_price keeps enough digits.
    from pipeline_core.utils import round_price
    assert round_price(0.0123) == 0.0123
    assert round_price(0.00004567) == 0.00004567
    assert round_price(64719.99) == 64719.99
    # A cheap coin now gets a distinct stop below entry, so it doesn't insta-close.
    idea = _idea(entry_price=0.0123, stop_price=round_price(0.0123 * 0.94),
                 target_price=round_price(0.0123 * 1.18))
    assert runner._evaluate_idea_path(idea, _hist([0.0123, 0.0124, 0.0125])) is None


def test_path_stop_before_target_same_bar():
    # A bar whose range spans both stop and target closes as a stop (conservative).
    idx = pd.date_range("2026-06-01", periods=2, freq="B")
    df = pd.DataFrame({"Open": [100, 100], "High": [100, 115], "Low": [100, 90],
                       "Close": [100, 100]}, index=idx)
    out = runner._evaluate_idea_path(_idea(), df)
    assert out["exit_reason"] == "stop"


# ── tracked_ideas_status: split + persist ───────────────────────────


@pytest.fixture
def tmp_tracked(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TRACKED_FILE", tmp_path / "tracked.json")
    # Open ideas are marked with a fresh live quote (runner._current_price) — stub it so the
    # tracker stays network-free and the open mark is deterministic.
    monkeypatch.setattr(runner, "_current_price", lambda tk, market="IN": 105.0)
    return tmp_path


def test_status_closes_and_persists(tmp_tracked, monkeypatch):
    # WIN hits its target; OPEN stays open.
    runner._save_tracked([
        _idea(ticker="WIN", target_price=110.0),
        _idea(ticker="OPEN", target_price=999.0, stop_price=1.0),
    ])
    histories = {"WIN": _hist([100, 105, 112]), "OPEN": _hist([100, 101, 102])}
    monkeypatch.setattr(runner, "get_price_history",
                        lambda tk, period, interval, mkt: histories[tk])

    res = asyncio.run(runner.tracked_ideas_status("CRYPTO"))

    assert [c["ticker"] for c in res["closed"]] == ["WIN"]
    assert [o["ticker"] for o in res["open"]] == ["OPEN"]
    assert res["closed_scoreboard"]["wins"] == 1 and res["closed_scoreboard"]["win_rate"] == 100.0
    assert res["equity_curve"] and res["equity_curve"][-1][1] == 10.0

    # The closure is persisted (idempotent) — a second pass keeps WIN closed without re-eval.
    stored = {t["ticker"]: t for t in runner._load_tracked()}
    assert stored["WIN"].get("closed") is True and stored["WIN"]["exit_reason"] == "target"
    assert not stored["OPEN"].get("closed")


def test_status_scoped_by_market_preserves_others(tmp_tracked, monkeypatch):
    runner._save_tracked([
        _idea(ticker="AAA", market="US", target_price=110.0),
        _idea(ticker="BBB", market="CRYPTO", target_price=110.0),
    ])
    monkeypatch.setattr(runner, "get_price_history",
                        lambda tk, period, interval, mkt: _hist([100, 106, 113]))
    asyncio.run(runner.tracked_ideas_status("US"))          # only US evaluated
    stored = {t["ticker"]: t for t in runner._load_tracked()}
    assert stored["AAA"].get("closed") is True              # US idea closed
    assert not stored["BBB"].get("closed")                  # crypto idea untouched


# ── horizon_ideas: drop closed, tag new, show age ───────────────────


def _latest(market="CRYPTO", sig=None):
    """Minimal backtest payload with one long-horizon (golden-cross) strategy flagging
    two BUY signals — enough for horizon_ideas to build a 'long' bucket."""
    sig = sig or datetime.now(timezone.utc).date().isoformat()
    return {
        "run_at": "2026-07-26T00:00:00Z", "market": market, "index": "IDX",
        "benchmark_label": "BENCH", "years": 10, "disclaimer": "d",
        "strategies": [{
            "id": "golden-cross", "name": "Golden Cross", "category": "Trend",
            "metrics": {"sharpe": 1.2, "win_rate": 55, "expectancy_pct": 2.0},
            "signals": {"as_of": sig, "sell": [], "holding": [], "buy": [
                {"ticker": "WIN", "price": 100.0, "signal_date": sig},
                {"ticker": "OPEN", "price": 50.0, "signal_date": sig},
            ]},
        }],
    }


def test_horizon_ideas_drops_closed_ideas(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    # WIN already closed in the tracker (a past trade); OPEN is still live.
    runner._save_tracked([_idea(ticker="WIN", market="CRYPTO", horizon="long", closed=True,
                                exit_reason="target")])
    bucket = runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]
    assert [c["ticker"] for c in bucket["candidates"]] == ["OPEN"]   # WIN dropped
    assert bucket["total_candidates"] == 1


def test_horizon_ideas_tags_new_and_age(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    runner._save_tracked([])
    cand = runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"][0]
    assert cand["age_days"] == 0 and cand["is_new"] is True          # signalled today


def test_horizon_ideas_stale_signal_not_new(tmp_tracked, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=runner.IDEA_NEW_DAYS + 5)).date().isoformat()
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO", sig=old))
    runner._save_tracked([])
    cand = runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"][0]
    assert cand["is_new"] is False and cand["age_days"] > runner.IDEA_NEW_DAYS


# ── Carry-over: open ideas persist on the board until they close ─────


def test_horizon_ideas_carries_open_but_not_closed(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    runner._save_tracked([
        _idea(ticker="OLD", market="CRYPTO", horizon="long", avg_win_rate=70,
              stop_pct=0.1, target_pct=0.3, signal_date="2026-06-01"),   # open, not re-signalled
        _idea(ticker="GONE", market="CRYPTO", horizon="long", closed=True, exit_reason="stop"),
    ])
    longc = runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"]
    tickers = {c["ticker"] for c in longc}
    assert "OLD" in tickers and "GONE" not in tickers          # open carried, closed dropped
    old = next(c for c in longc if c["ticker"] == "OLD")
    assert old["carried"] is True and old["avg_win_rate"] == 70


def test_horizon_ideas_higher_winrate_carried_outranks_fresh(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    runner._save_tracked([
        _idea(ticker="OLD", market="CRYPTO", horizon="long", conviction=5, avg_win_rate=80,
              stop_pct=0.1, target_pct=0.3, signal_date="2026-06-01"),
    ])
    longc = runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"]
    assert longc[0]["ticker"] == "OLD"                          # win-rate + conviction keep it on top


def test_horizon_ideas_dedupes_fresh_and_tracked(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    # WIN is both a fresh signal and already tracked-open → appears once (fresh, not carried).
    runner._save_tracked([_idea(ticker="WIN", market="CRYPTO", horizon="long")])
    wins = [c for c in runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"]
            if c["ticker"] == "WIN"]
    assert len(wins) == 1 and wins[0]["carried"] is False


# ── Re-entry: a closed idea that re-signals after its exit resurfaces ─


def test_reentry_resurfaces_on_board(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))   # WIN signals today
    runner._save_tracked([_idea(ticker="WIN", market="CRYPTO", horizon="long", closed=True,
                                exit_reason="target", exit_date="2026-06-15")])
    win = [c for c in runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"]
           if c["ticker"] == "WIN"]
    assert win and win[0]["carried"] is False           # re-entry shows as a fresh idea


def test_same_day_close_is_not_a_reentry(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    today = datetime.now(timezone.utc).date().isoformat()
    runner._save_tracked([_idea(ticker="WIN", market="CRYPTO", horizon="long", closed=True,
                                exit_date=today)])       # closed today; signal not strictly after
    longc = runner.horizon_ideas(market="CRYPTO")["buckets"]["long"]["candidates"]
    assert all(c["ticker"] != "WIN" for c in longc)      # stays off until it fires again later


def test_snapshot_tracks_reentry_as_new_cycle(tmp_tracked, monkeypatch):
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _latest("CRYPTO"))
    runner._save_tracked([_idea(ticker="WIN", market="CRYPTO", horizon="long", closed=True,
                                exit_date="2026-06-15")])
    added = runner.snapshot_current_ideas(market="CRYPTO")["added"]
    recs = [t for t in runner._load_tracked() if t["ticker"] == "WIN" and t["horizon"] == "long"]
    assert added >= 1 and len(recs) == 2                 # old closed trade + new open cycle
    assert any(not t.get("closed") for t in recs)        # the re-entry is open again


def test_days_since():
    assert runner._days_since(None) is None
    assert runner._days_since("not-a-date") is None
    assert runner._days_since(datetime.now(timezone.utc).date().isoformat()) == 0
    wk = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    assert runner._days_since(wk) == 7
