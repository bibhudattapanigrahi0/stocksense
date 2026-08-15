"""Best-Odds board — the day's fresh setups ranked PURELY by win probability.

Pure/offline: a function of numbers a board candidate already carries. Sibling of test_basket.py (both
distil the same enriched board — the basket into a balanced sized book, this into a win-rate ranking)."""

import pytest

from pipeline_core.backtest import basket


def _cand(ticker, **over):
    """A board candidate carrying the fields build_best_odds reads (post horizon_ideas + enrich)."""
    c = {
        "ticker": ticker, "price": 100.0,
        "buy_low": 99.0, "buy_high": 101.0,
        "stop_pct": 0.06, "stop_price": 94.0,
        "target_pct": 0.12, "target_price": 112.0,
        "risk_reward": 2.0,
        "conviction": 3, "eligible_conviction": 3,
        "avg_win_rate": 55.0, "rank_score": 3.0, "sector": None,
        "zone": "in_zone", "is_new": False,
    }
    c.update(over)
    return c


def _board(by_horizon, regime=None):
    horizons = ["short", "medium", "long"]
    buckets = {h: {"horizon": h, "label": h.title() + " term", "candidates": by_horizon.get(h, [])}
               for h in horizons}
    return {"market": "IN", "horizons": horizons, "buckets": buckets, "regime": regime}


# ── Ranking ─────────────────────────────────────────────────────────

def test_ranks_purely_by_win_probability():
    board = _board({"short": [_cand("LO", avg_win_rate=52.0), _cand("HI", avg_win_rate=78.0)],
                    "medium": [_cand("MID", avg_win_rate=65.0)]})
    b = basket.build_best_odds(board, None)
    order = [it["ticker"] for it in b["items"]]
    assert order == ["HI", "MID", "LO"]                          # strictly descending win prob
    assert [it["rank"] for it in b["items"]] == [1, 2, 3]
    assert b["items"][0]["win_prob"] == 78.0


def test_win_prob_source_precedence_drives_the_number():
    # An idea with a learned model win prob (0.62) *and* a rosy advertised rate (95) is ranked on the
    # MODEL number (62), so a plain advertised-70 idea outranks it — precedence changes what's compared.
    board = _board({"short": [
        _cand("MODEL", model_win_pct=0.62, avg_win_rate=95.0),
        _cand("ADV", avg_win_rate=70.0),
    ]})
    b = basket.build_best_odds(board, None)
    by = {it["ticker"]: it for it in b["items"]}
    assert by["MODEL"]["win_prob"] == 62.0 and by["MODEL"]["win_prob_source"] == "learned"
    assert by["ADV"]["win_prob"] == 70.0 and by["ADV"]["win_prob_source"] == "advertised"
    assert [it["ticker"] for it in b["items"]] == ["ADV", "MODEL"]


# ── De-duplication (distinctness, not ranking) ──────────────────────

def test_ticker_dedup_keeps_the_best_copy():
    # The same name surfaces in two horizons; only its highest-win-prob copy is kept, once.
    board = _board({"short": [_cand("DUP", avg_win_rate=80.0)],
                    "long": [_cand("DUP", avg_win_rate=60.0), _cand("OTHER", avg_win_rate=70.0)]})
    b = basket.build_best_odds(board, None)
    tickers = [it["ticker"] for it in b["items"]]
    assert tickers.count("DUP") == 1
    dup = next(it for it in b["items"] if it["ticker"] == "DUP")
    assert dup["win_prob"] == 80.0 and dup["horizon"] == "short"   # the better copy


def test_cluster_dedup_toggle(monkeypatch):
    clusters = {"AAA": "c1", "BBB": "c1", "CCC": "c2"}             # AAA & BBB are correlated
    board = _board({"short": [_cand("AAA", avg_win_rate=82.0), _cand("BBB", avg_win_rate=75.0),
                              _cand("CCC", avg_win_rate=68.0)]})
    monkeypatch.setattr(basket.settings, "best_odds_cluster_dedup", True)
    on = [it["ticker"] for it in basket.build_best_odds(board, clusters)["items"]]
    assert on == ["AAA", "CCC"]                                    # BBB dropped (same cluster as AAA)
    monkeypatch.setattr(basket.settings, "best_odds_cluster_dedup", False)
    off = [it["ticker"] for it in basket.build_best_odds(board, clusters)["items"]]
    assert off == ["AAA", "BBB", "CCC"]                            # all kept


def test_top_n_cap():
    cands = [_cand(f"T{i}", avg_win_rate=50.0 + i) for i in range(8)]
    board = _board({"short": cands})
    b = basket.build_best_odds(board, None, top_n=3)
    assert len(b["items"]) == 3 and b["size"] == 3
    assert b["items"][0]["win_prob"] == 57.0                       # highest (50+7)


# ── Freshness / summary / guards ────────────────────────────────────

def test_spent_names_excluded():
    board = _board({"short": [_cand("STOPPED", avg_win_rate=90.0, zone="below_stop"),
                              _cand("DONE", avg_win_rate=88.0, zone="at_target"),
                              _cand("FRESH", avg_win_rate=60.0, zone="in_zone")]})
    b = basket.build_best_odds(board, None)
    assert [it["ticker"] for it in b["items"]] == ["FRESH"]        # spent plans aren't fresh entries


def test_summary_source_tally_and_backed_count():
    board = _board({"short": [
        _cand("A", model_win_pct=0.71), _cand("B", calibrated_win_pct=0.66),
        _cand("C", avg_win_rate=64.0), _cand("D", avg_win_rate=58.0),
    ]})
    s = basket.build_best_odds(board, None)["summary"]
    assert s["count"] == 4
    assert s["win_prob_sources"] == {"learned": 1, "calibrated": 1, "advertised": 2}
    assert s["backed_count"] == 2                                  # learned + calibrated
    assert s["avg_win_pct"] == pytest.approx((71 + 66 + 64 + 58) / 4, abs=0.1)


def test_empty_and_disabled(monkeypatch):
    empty = basket.build_best_odds(_board({}), None)
    assert empty["enabled"] is True and empty["items"] == []
    monkeypatch.setattr(basket.settings, "best_odds_enabled", False)
    off = basket.build_best_odds(_board({"short": [_cand("X")]}), None)
    assert off["items"] == [] and "disabled" in off["note"].lower()
