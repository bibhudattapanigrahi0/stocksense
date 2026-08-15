"""Sell-side smart-money surfacing — the distribution signal the pipeline already SCORES (negative for
longs, inverted-positive for shorts) is now made VISIBLE and ACTIONABLE:

  1A  a distribution flag on ideas (an AVOID warning on a long, a CONFIRM tailwind on a crypto short),
  1B  the coattail SHORT sleeve — SHORT ideas the smart money is DISTRIBUTING, sized as an inverted book,
  1C  a held-position EXIT nudge when the smart money turns against an open trade.

All network-free: pure helpers on synthetic signals + a `tracked_ideas_status` path with the price/history
and smart-money fetches monkeypatched. `conftest` already neutralizes `smart_money.get_signal` offline;
the async test opts back in with its own monkeypatch (which runs after the autouse fixture)."""

import asyncio

import pandas as pd
import pytest

from pipeline_core.backtest import basket, idea_ranking, runner
from pipeline_core.config import settings


def _sig(direction, family="flow", **o):
    return {"available": True, "direction": direction, "family": family,
            "label": "🐋 whales", "detail": "whales net -$50m short", **o}


# ── 1A. distribution_flag ────────────────────────────────────────────

def test_distribution_flag_long_is_an_avoid_warning():
    f = idea_ranking.distribution_flag(_sig("distribute"), side="long")
    assert f and f["stance"] == "avoid"
    assert "🐋" in f["label"] and f["detail"]


def test_distribution_flag_short_is_a_confirmation():
    f = idea_ranking.distribution_flag(_sig("distribute"), side="short")
    assert f and f["stance"] == "confirm"


def test_distribution_flag_none_on_accumulate_or_missing():
    assert idea_ranking.distribution_flag(_sig("accumulate")) is None
    assert idea_ranking.distribution_flag(None) is None
    assert idea_ranking.distribution_flag({"available": False, "direction": "distribute"}) is None


def test_distribution_flag_labels_per_family():
    assert "13F" in idea_ranking.distribution_flag(_sig("distribute", family="ownership"))["label"]
    assert "COT" in idea_ranking.distribution_flag(_sig("distribute", family="cot"))["label"]


# ── 1C. smart_money_exit_flag ────────────────────────────────────────

def test_exit_flag_fires_when_pros_distribute_a_held_long():
    f = idea_ranking.smart_money_exit_flag(_sig("distribute"), side="long")
    assert f and "trimming" in f["message"].lower()


def test_exit_flag_fires_when_pros_accumulate_a_held_short():
    f = idea_ranking.smart_money_exit_flag(_sig("accumulate"), side="short")
    assert f and "covering" in f["message"].lower()


def test_exit_flag_silent_when_pros_agree_with_the_position():
    assert idea_ranking.smart_money_exit_flag(_sig("accumulate"), side="long") is None
    assert idea_ranking.smart_money_exit_flag(_sig("distribute"), side="short") is None
    assert idea_ranking.smart_money_exit_flag(None, side="long") is None


# ── 1B. coattail SHORT sleeve (basket.distribute_mode) ───────────────

def _cand(t, side, direction, conv=3):
    return {"ticker": t, "side": side, "conviction": conv, "eligible_conviction": conv,
            "price": 100.0, "stop_pct": 0.1, "target_pct": 0.2, "risk_reward": 2.0,
            "win_prob": 45.0, "expected_return_pct": 3.0, "rank_score": conv,
            "smart_money": _sig(direction)}


def _board(regime, cands):
    return {"market": "CRYPTO", "regime": regime, "horizons": ["short"],
            "buckets": {"short": {"label": "Short term", "candidates": cands}}}


def test_short_sleeve_keeps_only_distributing_shorts():
    board = _board({"state": "risk-off", "tilt": -0.5}, [
        _cand("BTC", "long", "accumulate"),      # long → excluded from the short sleeve
        _cand("HTX", "short", "distribute"),     # short + whales distributing → KEPT
        _cand("XRP", "short", "accumulate"),     # short but whales accumulating → excluded
    ])
    b = basket.build_basket(board, {}, distribute_mode=True)
    assert b["distribute_mode"] is True
    tks = {it["ticker"] for it in b["items"]}
    assert tks == {"HTX"}
    assert b["summary"]["smart_money_backed"] == 1
    assert all(it["side"] == "short" for it in b["items"])


def test_short_sleeve_ranks_by_conviction_and_sizes():
    board = _board({"state": "risk-off", "tilt": -0.4},
                   [_cand(f"C{i}", "short", "distribute", conv=i) for i in range(1, 5)])
    b = basket.build_basket(board, {}, distribute_mode=True)
    assert b["items"] and b["items"][0]["ticker"] == "C4"          # strongest agreement leads
    assert all((it.get("alloc_pct") or 0) > 0 for it in b["items"])  # sized as a book


def test_short_sleeve_empty_when_no_distributing_shorts():
    board = _board({"state": "risk-on", "tilt": 0.5}, [_cand("BTC", "long", "accumulate")])
    b = basket.build_basket(board, {}, distribute_mode=True)
    assert b["items"] == [] and "short" in b["note"].lower()


def test_short_sleeve_size_inverts_with_regime():
    # A risk-OFF tide is a tailwind for shorts → the sleeve should surface MORE of them, not fewer.
    shorts = [_cand(f"C{i}", "short", "distribute", conv=3) for i in range(8)]
    off = basket.build_basket(_board({"state": "risk-off", "tilt": -0.6}, shorts), {}, distribute_mode=True)
    on = basket.build_basket(_board({"state": "risk-on", "tilt": 0.6}, shorts), {}, distribute_mode=True)
    assert off["size"] == settings.basket_size_max      # risk-off inverted → max shorts
    assert on["size"] == settings.basket_size_min       # risk-on inverted → min shorts


def test_pool_side_filter():
    board = _board({"state": "neutral"}, [_cand("A", "long", "accumulate"), _cand("S", "short", "distribute")])
    assert {c["ticker"] for c in basket._pool(board, side="long")} == {"A"}
    assert {c["ticker"] for c in basket._pool(board, side="short")} == {"S"}


def test_long_coattail_unaffected_by_the_short_sleeve():
    board = _board({"state": "risk-on", "tilt": 0.3},
                   [_cand("BTC", "long", "accumulate"), _cand("HTX", "short", "distribute")])
    lb = basket.build_basket(board, {}, coattail_mode=True)
    assert {it["ticker"] for it in lb["items"]} == {"BTC"}    # long accumulate only; short excluded


# ── 1C. tracked_ideas_status attaches the exit flag (async, mocked) ───

def _open_hist():
    idx = pd.date_range("2026-08-01", periods=5, freq="B")
    c = pd.Series([100, 101, 102, 101, 102], index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c}, index=idx)


def test_tracked_status_flags_a_held_long_the_pros_are_distributing(monkeypatch):
    held = {"ticker": "HTX", "market": "CRYPTO", "horizon": "medium", "side": "long",
            "entry_price": 100.0, "stop_price": 90.0, "target_price": 130.0,
            "signal_date": "2026-08-01", "tracked_at": "2026-08-01T00:00:00+00:00", "closed": False}
    monkeypatch.setattr(runner, "_load_tracked", lambda: [held])
    monkeypatch.setattr(runner, "get_price_history", lambda tk, period, interval, mkt: _open_hist())
    monkeypatch.setattr(runner, "_current_price", lambda tk, mkt: 102.0)
    monkeypatch.setattr(runner.smart_money, "get_signal", lambda mkt, tk: _sig("distribute"))

    out = asyncio.run(runner.tracked_ideas_status("CRYPTO"))
    assert len(out["open"]) == 1
    x = out["open"][0]["smart_money_exit"]
    assert x and "trimming" in x["message"].lower()


def test_tracked_status_no_exit_flag_when_pros_still_accumulate(monkeypatch):
    held = {"ticker": "BTC", "market": "CRYPTO", "horizon": "medium", "side": "long",
            "entry_price": 100.0, "stop_price": 90.0, "target_price": 130.0,
            "signal_date": "2026-08-01", "tracked_at": "2026-08-01T00:00:00+00:00", "closed": False}
    monkeypatch.setattr(runner, "_load_tracked", lambda: [held])
    monkeypatch.setattr(runner, "get_price_history", lambda tk, period, interval, mkt: _open_hist())
    monkeypatch.setattr(runner, "_current_price", lambda tk, mkt: 102.0)
    monkeypatch.setattr(runner.smart_money, "get_signal", lambda mkt, tk: _sig("accumulate"))

    out = asyncio.run(runner.tracked_ideas_status("CRYPTO"))
    assert out["open"][0]["smart_money_exit"] is None
