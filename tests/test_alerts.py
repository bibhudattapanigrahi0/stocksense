"""Alerts + watchlist + digest — persistence, fingerprint dedupe, and the trigger engine.
Network-free (tmp DB, mocked quotes + ledger)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline_core import alerts
from pipeline_core.backtest import runner
from pipeline_core.services import run_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolate every test on its own SQLite file (mirrors the portfolio-tracking tests)."""
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "alerts.db")
    return tmp_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _no_tracked(market):
    return {"closed": [], "open": []}


# ── watchlist persistence ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchlist_crud(tmp_db):
    w = await run_store.add_watch({"ticker": "reliance", "market": "in", "level_above": 3000})
    assert w["ticker"] == "RELIANCE" and w["market"] == "IN" and w["id"]      # normalized upper
    items = await run_store.list_watch("IN")
    assert len(items) == 1 and items[0]["ticker"] == "RELIANCE"
    assert await run_store.list_watch("US") == []                              # market-scoped
    assert await run_store.delete_watch(w["id"]) is True
    assert await run_store.list_watch("IN") == []


# ── alert store: dedupe / read / clear ──────────────────────────────


@pytest.mark.asyncio
async def test_alert_save_dedupe_read_clear(tmp_db):
    assert await run_store.save_alert(alerts._alert("IN", "test", "T", "b", fingerprint="fp")) is not None
    assert await run_store.save_alert(alerts._alert("IN", "test", "T", "b", fingerprint="fp")) is None   # duplicate
    assert await run_store.save_alert(alerts._alert("IN", "test", "T2", "b2", fingerprint="fp2")) is not None
    assert await run_store.unread_count("IN") == 2
    assert len(await run_store.list_alerts("IN", unread_only=True)) == 2
    assert await run_store.mark_alerts_read(market="IN") == 2
    assert await run_store.unread_count("IN") == 0
    assert await run_store.clear_alerts("IN") == 2
    assert await run_store.list_alerts("IN") == []


# ── engine: watchlist price level ───────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_watch_level_fires_once(tmp_db, monkeypatch):
    await run_store.add_watch({"ticker": "AAA", "market": "IN", "level_above": 100})
    monkeypatch.setattr(alerts.market_data, "get_quote", lambda t, m, max_age=None: 105.0)
    monkeypatch.setattr(runner, "tracked_ideas_status", _no_tracked)
    monkeypatch.setattr(runner, "horizon_ideas", lambda market=None: None)

    s1 = await alerts.evaluate_market("IN")
    assert s1["emitted"] == 1 and s1["by_type"].get("watch_level") == 1
    s2 = await alerts.evaluate_market("IN")          # same level, same day → fingerprint-deduped
    assert s2["emitted"] == 0


# ── engine: tracked-idea closures (recent only) ─────────────────────


@pytest.mark.asyncio
async def test_evaluate_idea_closed_recent_only(tmp_db, monkeypatch):
    monkeypatch.setattr(alerts.market_data, "get_quote", lambda *a, **k: None)
    recent = {"ticker": "WIN", "closed_at": _now_iso(), "exit_date": "2026-07-30",
              "close_reason": "target", "realized_return_pct": 8.0}
    old = {"ticker": "OLD", "closed_at": "2020-01-01T00:00:00+00:00", "exit_date": "2020-01-01",
           "close_reason": "stop", "realized_return_pct": -5.0}

    async def _tracked(market):
        return {"closed": [recent, old], "open": []}

    monkeypatch.setattr(runner, "tracked_ideas_status", _tracked)
    monkeypatch.setattr(runner, "horizon_ideas", lambda market=None: None)

    s = await alerts.evaluate_market("IN")
    assert s["by_type"].get("idea_closed") == 1               # the stale 2020 closure is filtered out
    win = [a for a in await run_store.list_alerts("IN") if a["ticker"] == "WIN"][0]
    assert win["severity"] == "good" and "target" in win["body"].lower()


# ── engine: fresh ideas only for watched tickers ────────────────────


@pytest.mark.asyncio
async def test_evaluate_new_idea_for_watched_only(tmp_db, monkeypatch):
    await run_store.add_watch({"ticker": "WATCHED", "market": "IN"})
    monkeypatch.setattr(alerts.market_data, "get_quote", lambda *a, **k: None)
    monkeypatch.setattr(runner, "tracked_ideas_status", _no_tracked)
    ideas = {"horizons": ["short"], "buckets": {"short": {"candidates": [
        {"ticker": "WATCHED", "is_new": True, "conviction": 3, "avg_win_rate": 60, "latest_signal_date": "2026-07-30"},
        {"ticker": "OTHER", "is_new": True, "conviction": 5, "avg_win_rate": 70, "latest_signal_date": "2026-07-30"},
    ]}}}
    monkeypatch.setattr(runner, "horizon_ideas", lambda market=None: ideas)

    s = await alerts.evaluate_market("IN")
    assert s["by_type"].get("new_idea") == 1
    assert [a["ticker"] for a in await run_store.list_alerts("IN") if a["type"] == "new_idea"] == ["WATCHED"]


# ── engine: hold-coaching (beyond the normal-dip band) ──────────────


@pytest.mark.asyncio
async def test_evaluate_hold_coach_beyond_band(tmp_db, monkeypatch):
    monkeypatch.setattr(alerts.market_data, "get_quote", lambda *a, **k: None)

    async def _tracked(market):
        return {"closed": [], "open": [
            {"ticker": "DIP", "dd_status": "beyond", "dd_pct": 12.0,
             "typical_drawdown_pct": 7.0, "deep_drawdown_pct": 10.0},
            {"ticker": "OK", "dd_status": "within", "dd_pct": 3.0},        # within band → no alert
        ]}

    monkeypatch.setattr(runner, "tracked_ideas_status", _tracked)
    monkeypatch.setattr(runner, "horizon_ideas", lambda market=None: None)

    s1 = await alerts.evaluate_market("IN")
    assert s1["by_type"].get("hold_coach") == 1                            # only DIP (beyond)
    coach = [a for a in await run_store.list_alerts("IN") if a["type"] == "hold_coach"][0]
    assert coach["ticker"] == "DIP" and coach["severity"] == "warn"
    s2 = await alerts.evaluate_market("IN")                                # same day → deduped
    assert s2["by_type"].get("hold_coach", 0) == 0


# ── engine: buy-zone "triggered" nudge for watched tickers ──────────


@pytest.mark.asyncio
async def test_evaluate_idea_triggered_in_buy_zone(tmp_db, monkeypatch):
    await run_store.add_watch({"ticker": "TRIG", "market": "IN"})
    monkeypatch.setattr(alerts.market_data, "get_quote", lambda *a, **k: 100.0)   # inside the buy zone
    monkeypatch.setattr(runner, "tracked_ideas_status", _no_tracked)
    zone = {"buy_low": 98.0, "buy_high": 102.0, "stop_price": 94.0, "target_price": 118.0}
    ideas = {"horizons": ["short"], "buckets": {"short": {"candidates": [
        {"ticker": "TRIG", "is_new": False, "conviction": 3, **zone, "latest_signal_date": "2026-07-30"},
        {"ticker": "OTHER", "is_new": False, "conviction": 4, **zone},     # not watched → ignored
    ]}}}
    monkeypatch.setattr(runner, "horizon_ideas", lambda market=None: ideas)

    s = await alerts.evaluate_market("IN")
    assert s["by_type"].get("idea_triggered") == 1
    trig = [a for a in await run_store.list_alerts("IN") if a["type"] == "idea_triggered"]
    assert [a["ticker"] for a in trig] == ["TRIG"]


# ── digest ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_digest_rolls_up_window(tmp_db, monkeypatch):
    await run_store.save_alert(alerts._alert("IN", "watch_level", "T", "b", fingerprint="x"))

    async def _tracked(market):
        return {"closed": [{"ticker": "W", "closed_at": _now_iso(), "realized_return_pct": 5.0},
                           {"ticker": "L", "closed_at": _now_iso(), "realized_return_pct": -3.0}],
                "open": [{"ticker": "O"}]}

    monkeypatch.setattr(runner, "tracked_ideas_status", _tracked)
    d = await alerts.build_digest("IN")
    assert d["alert_count"] == 1
    assert d["closed"] == {"total": 2, "wins": 1, "losses": 1}
    assert d["open_ideas"] == 1
