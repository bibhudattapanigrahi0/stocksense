"""Curated cross-horizon basket — scoring, greedy diversified selection, hysteresis, sizing.

Pure/offline: everything is a function of numbers a board candidate already carries. Mirrors the
intraday-shortlist tests (test_intraday_ideas_selection.py) for the daily analog (basket.py)."""

import pytest

from pipeline_core.backtest import basket
from pipeline_core.config import settings


# ── Fixtures: build a synthetic enriched board ──────────────────────

def _cand(ticker, **over):
    """A board candidate carrying the fields basket.py reads (post horizon_ideas + enrich)."""
    c = {
        "ticker": ticker, "price": 100.0,
        "buy_low": 99.0, "buy_high": 101.0,
        "stop_pct": 0.06, "stop_price": 94.0,
        "target_pct": 0.12, "target_price": 112.0,
        "risk_reward": 2.0,
        "conviction": 3, "eligible_conviction": 3,
        "avg_win_rate": 55.0, "avg_sharpe": 1.0, "avg_max_drawdown": -20.0,
        "rank_score": 3.0, "sector": None, "zone": "in_zone", "is_new": False,
    }
    c.update(over)
    return c


def _board(by_horizon, regime=None):
    horizons = ["short", "medium", "long"]
    buckets = {}
    for h in horizons:
        buckets[h] = {"horizon": h, "label": h.title() + " term", "candidates": by_horizon.get(h, [])}
    return {"market": "IN", "horizons": horizons, "buckets": buckets, "regime": regime}


# ── Per-idea math ───────────────────────────────────────────────────

def test_expected_value_math():
    # p=0.55, target=+12%, stop=−6% → 0.55·0.12 − 0.45·0.06 = 0.039
    assert basket.expected_value(0.55, 0.12, 0.06) == pytest.approx(0.039)
    assert basket.expected_value(None, 0.12, 0.06) is None      # no win prob → no EV
    assert basket.expected_value(0.5, None, 0.06) is None       # no plan geometry → no EV


def test_ev_term_squash():
    assert basket.ev_term(0.0) == 0.5                           # break-even → 0.5
    assert basket.ev_term(0.05) > 0.5                           # +5% EV lifts
    assert basket.ev_term(-0.05) < 0.5                          # negative EV trims
    assert basket.ev_term(None) == 0.40                         # no EV sits just below neutral


def test_confidence_term_diminishing_and_lift():
    assert basket.confidence_term(1) == pytest.approx(0.45)     # 1 strategy
    assert basket.confidence_term(2) == pytest.approx(0.6975)   # diminishing returns (1 − 0.55²)
    assert basket.confidence_term(3) == pytest.approx(0.8336, abs=1e-3)
    # A learned/calibrated probability backing the win rate lifts confidence.
    assert basket.confidence_term(3, has_model=True) > basket.confidence_term(3)
    assert (basket.confidence_term(3, has_model=True)
            > basket.confidence_term(3, has_calibration=True) > basket.confidence_term(3))
    assert basket.confidence_term(0) == pytest.approx(0.0)


def test_weights_normalized_to_one():
    w = basket.basket_weights()
    assert round(w["ev"] + w["confidence"] + w["quality"], 6) == 1.0


def test_basket_size_regime_scaled():
    assert basket._basket_size({"state": "risk-on"}) == settings.basket_size_max
    assert basket._basket_size({"state": "risk-off"}) == settings.basket_size_min
    mid = (settings.basket_size_min + settings.basket_size_max) // 2
    assert basket._basket_size({"state": "neutral"}) == mid
    assert basket._basket_size(None) == mid                     # offline → neutral midpoint


# ── Greedy diversified selection ────────────────────────────────────

def _score(c):
    return c["basket_score"]


def test_greedy_hard_cluster_dedup():
    # A and B are correlated (same cluster 0); C is independent (cluster 1). With size 2 the basket
    # must never hold two names from the same cluster — it takes the better of A/B, then C.
    a = _cand("A", basket_score=0.90, horizon="short")
    b = _cand("B", basket_score=0.85, horizon="short")
    c = _cand("C", basket_score=0.80, horizon="short")
    clusters = {"A": 0, "B": 0, "C": 1}
    picks = basket.greedy_select([a, b, c], clusters, size=2, sector_cap=2, effective_score=_score)
    tickers = [p["ticker"] for p in picks]
    assert tickers == ["A", "C"]                                # B (A's correlated peer) skipped


def test_greedy_soft_sector_cap():
    # Three top names in "Tech" (cap 2) + one in "Energy". Cap defers the 3rd Tech in favour of Energy.
    tech = [_cand(t, basket_score=s, horizon="short", sector="Tech")
            for t, s in (("T1", 0.95), ("T2", 0.90), ("T3", 0.88))]
    eng = _cand("E1", basket_score=0.70, horizon="short", sector="Energy")
    picks = basket.greedy_select(tech + [eng], {}, size=3, sector_cap=2, effective_score=_score)
    tickers = {p["ticker"] for p in picks}
    assert tickers == {"T1", "T2", "E1"}                        # 2 Tech + Energy, T3 deferred by the cap


def test_greedy_horizon_spread_tiebreak():
    # Within a score tie-band the greedy prefers an unrepresented horizon (soft spread).
    a = _cand("A", basket_score=0.80, horizon="short")
    b = _cand("B", basket_score=0.795, horizon="short")        # ~tied with A
    c = _cand("C", basket_score=0.79, horizon="long")          # ~tied, different horizon
    picks = basket.greedy_select([a, b, c], {}, size=2, sector_cap=2, effective_score=_score)
    assert [p["ticker"] for p in picks] == ["A", "C"]          # long C beats short B on the spread nudge


# ── End-to-end build: sizing, summary, copies ───────────────────────

def test_build_basket_sizes_one_book_and_summarizes():
    board = _board({
        "short": [_cand("AAA", rank_score=4.0), _cand("BBB", rank_score=3.0)],
        "medium": [_cand("CCC", rank_score=3.5)],
        "long": [_cand("DDD", rank_score=2.0)],
    }, regime={"state": "neutral", "tilt": 0.0})
    b = basket.build_basket(board, clusters={})
    assert b["enabled"] and b["items"]
    # Cross-horizon book: allocations sum to ≤100% (one shared budget, not one per horizon).
    total = sum(it["alloc_pct"] for it in b["items"])
    assert total == pytest.approx(b["summary"]["total_alloc_pct"], abs=0.2)
    assert total <= 100.1
    assert b["summary"]["cash_pct"] == pytest.approx(round(100 - total, 1), abs=0.2)
    # Neutral regime → size = midpoint; every item carries a win prob + expected return + score.
    assert b["summary"]["count"] == basket._basket_size(board["regime"])
    for it in b["items"]:
        assert it["expected_return_pct"] is not None
        assert it["win_prob"] is not None and it["win_prob_source"] == "advertised"
        assert 0.0 <= it["basket_score"] <= 1.0


def test_build_basket_excludes_spent_plans():
    # A name whose live price already broke its stop / hit its target is a bad fresh entry → excluded
    # from the basket (but counted in summary.excluded_spent). AAA is the top pick but is spent.
    board = _board({"short": [_cand("AAA", rank_score=9.0, zone="below_stop"),
                              _cand("BBB", rank_score=3.0, zone="in_zone"),
                              _cand("CCC", rank_score=2.0, zone="at_target")]})
    b = basket.build_basket(board, clusters={})
    tickers = {it["ticker"] for it in b["items"]}
    assert "AAA" not in tickers and "CCC" not in tickers    # spent plans dropped
    assert "BBB" in tickers
    assert b["summary"]["excluded_spent"] == 2


def test_build_basket_does_not_mutate_board_candidates():
    orig = _cand("ZZZ", rank_score=4.0)
    board = _board({"short": [orig]})
    basket.build_basket(board, clusters={})
    # Basket works on COPIES — the board row below is untouched (no basket_score / alloc bleed-through).
    assert "basket_score" not in orig and "alloc_pct" not in orig and "carried" not in orig


def test_build_basket_empty_and_disabled(monkeypatch):
    assert basket.build_basket(None)["items"] == []
    assert basket.build_basket(_board({}))["items"] == []       # board present but no candidates
    monkeypatch.setattr(basket.settings, "basket_enabled", False)
    off = basket.build_basket(_board({"short": [_cand("AAA")]}))
    assert off["items"] == [] and "disabled" in off["note"].lower()


def test_regime_risk_off_haircut_trims_book():
    cands = {"short": [_cand("AAA", rank_score=4.0), _cand("BBB", rank_score=3.0),
                       _cand("CCC", rank_score=2.0)]}
    on = basket.build_basket(_board(cands, regime={"state": "risk-on", "tilt": 0.6}), clusters={})
    off = basket.build_basket(_board(cands, regime={"state": "risk-off", "tilt": -0.6}), clusters={})
    # Risk-off runs a smaller basket AND a trimmed risk budget vs risk-on.
    assert off["summary"]["count"] <= on["summary"]["count"]
    assert off["sizing"].get("regime_size_factor", 1.0) < 1.0


# ── Hysteresis (sticky basket) ──────────────────────────────────────

@pytest.fixture
def _size_one(monkeypatch):
    """Pin the basket to a single slot so hysteresis must DECIDE between X and Y (with size ≥ the
    candidate count nothing is ever dropped, so incumbency can't be exercised)."""
    monkeypatch.setattr(basket.settings, "basket_size_min", 1)
    monkeypatch.setattr(basket.settings, "basket_size_max", 1)


def _two_name_board(**over):
    # X first, then Y — identical scoring inputs unless overridden, different names so only incumbency
    # breaks the tie. Single-horizon so ordering is deterministic.
    x = _cand("X", horizon="short")
    y = _cand("Y", horizon="short", **over)
    return _board({"short": [x, y]}, regime={"state": "neutral", "tilt": 0.0})


def test_hysteresis_retains_valid_incumbent(_size_one):
    board = _two_name_board()                                   # X and Y score equally
    fresh = basket.build_basket(board, clusters={})
    assert [it["ticker"] for it in fresh["items"]] == ["X"]     # no history → first wins
    # With Y as the committed incumbent, the bonus keeps Y despite X's (equal) raw score.
    prev = {"items": [{"ticker": "Y", "horizon": "short"}]}
    sticky = basket.build_basket(board, clusters={}, prev_basket=prev)
    assert [it["ticker"] for it in sticky["items"]] == ["Y"]
    assert sticky["items"][0]["carried"] is True


def test_hysteresis_challenger_displaces_only_past_margin(_size_one):
    # X is materially stronger (higher rank_score + win rate) → beats incumbent Y past the margin.
    board = _board({"short": [_cand("X", horizon="short", rank_score=8.0, avg_win_rate=70.0),
                              _cand("Y", horizon="short", rank_score=2.0, avg_win_rate=45.0)]},
                   regime={"state": "neutral", "tilt": 0.0})
    prev = {"items": [{"ticker": "Y", "horizon": "short"}]}
    b = basket.build_basket(board, clusters={}, prev_basket=prev)
    assert [it["ticker"] for it in b["items"]] == ["X"]         # challenger clearly better → swap in


def test_hysteresis_drops_broken_incumbent(_size_one):
    # Y has hit its stop (zone below_stop) → not a valid incumbent → no bonus → X (first) wins.
    board = _two_name_board(zone="below_stop")
    prev = {"items": [{"ticker": "Y", "horizon": "short"}]}
    b = basket.build_basket(board, clusters={}, prev_basket=prev)
    assert [it["ticker"] for it in b["items"]] == ["X"]
    assert all(not it.get("carried") for it in b["items"])


# ── Coattail basket: keep only names the smart money is accumulating ─────────

def test_coattail_basket_keeps_only_smart_money_backed():
    board = _board({
        "short": [_cand("AAA", smart_money={"direction": "accumulate"}), _cand("BBB")],  # BBB: no signal
        "medium": [_cand("CCC", smart_money={"direction": "distribute"}),                # measured, not buying
                   _cand("DDD", smart_money={"direction": "accumulate"})],
    })
    b = basket.build_basket(board, clusters={}, coattail_mode=True)
    assert {it["ticker"] for it in b["items"]} == {"AAA", "DDD"}
    assert b["coattail_mode"] is True and b["summary"]["smart_money_backed"] == 2


def test_coattail_basket_empty_when_no_backing():
    board = _board({"short": [_cand("AAA"), _cand("BBB", smart_money={"direction": "distribute"})]})
    b = basket.build_basket(board, clusters={}, coattail_mode=True)
    assert b["items"] == [] and "smart-money backing" in b["note"]
