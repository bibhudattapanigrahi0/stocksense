"""Position sizing — capped fractional-Kelly turns each idea's edge (win prob + reward:risk)
into a per-trade risk % and a portfolio allocation %. Pure math, network-free."""

from __future__ import annotations

import pytest

from pipeline_core import sizing

# Explicit params so tests don't depend on live settings.
P = dict(kelly_frac=0.25, max_position_pct=0.20, risk_per_trade_pct=0.015,
         max_book_risk_pct=0.06, enabled=True)


# ── kelly_fraction ──────────────────────────────────────────────────


def test_kelly_basic_formula():
    # p=0.6, b=2 → 0.6 − 0.4/2 = 0.4
    assert sizing.kelly_fraction(0.6, 2.0) == pytest.approx(0.4)


def test_kelly_accepts_percentage_or_fraction():
    assert sizing.kelly_fraction(60, 2.0) == pytest.approx(sizing.kelly_fraction(0.6, 2.0))


def test_kelly_non_positive_edge_is_zero():
    assert sizing.kelly_fraction(0.3, 1.0) == 0.0    # 0.3 − 0.7 = −0.4 → floored
    assert sizing.kelly_fraction(0.5, 0.5) == 0.0    # 0.5 − 0.5/0.5 = −0.5 → floored


@pytest.mark.parametrize("p,b", [(None, 2.0), (0.6, None), (0.6, 0), (0.6, -1), (float("nan"), 2.0)])
def test_kelly_invalid_inputs_zero(p, b):
    assert sizing.kelly_fraction(p, b) == 0.0


def test_kelly_clamped_to_unit_interval():
    assert 0.0 <= sizing.kelly_fraction(0.99, 100.0) <= 1.0


def test_kelly_monotonic_in_p_and_b():
    assert sizing.kelly_fraction(0.7, 2.0) > sizing.kelly_fraction(0.6, 2.0)
    assert sizing.kelly_fraction(0.6, 3.0) > sizing.kelly_fraction(0.6, 1.5)


# ── size_book: guards ───────────────────────────────────────────────


def test_disabled_leaves_ideas_unsized():
    ideas = [{"est_win_pct": 60, "r_multiple": 2.0, "entry": 100, "stop": 98}]
    summ = sizing.size_book(ideas, **{**P, "enabled": False})
    assert summ["enabled"] is False and summ["sized"] == 0
    assert "risk_pct" not in ideas[0] and "alloc_pct" not in ideas[0]


def test_empty_book():
    assert sizing.size_book([], **P)["sized"] == 0


# ── size_book: bounds & budget (the safety contract) ────────────────


def _mixed_book():
    return [
        {"est_win_pct": 64, "r_multiple": 2.4, "entry": 100, "stop": 98, "conviction": 4},
        {"est_win_pct": 58, "r_multiple": 2.0, "entry": 100, "stop": 96, "conviction": 3},
        {"est_win_pct": 55, "r_multiple": 1.8, "entry": 100, "stop": 97, "conviction": 2},
        {"est_win_pct": 52, "r_multiple": 1.5, "entry": 100, "stop": 95, "conviction": 2},
        {"est_win_pct": 40, "r_multiple": 1.2, "entry": 100, "stop": 93, "conviction": 1},  # neg edge: 40% < 45.5% breakeven at 1.2R
    ]


def test_book_respects_risk_and_allocation_budgets():
    ideas = _mixed_book()
    summ = sizing.size_book(ideas, **P)
    assert summ["sized"] == 5
    # Whole-book risk never exceeds the budget; allocation never exceeds 100%.
    assert summ["total_risk_pct"] <= 6.0 + 1e-9
    assert summ["total_alloc_pct"] <= 100.0 + 1e-9
    # Per-idea caps hold.
    for i in ideas:
        assert 0.0 <= i["risk_pct"] <= 1.5 + 1e-9
        assert 0.0 <= i["alloc_pct"] <= 20.0 + 1e-9
        assert set(sizing.SIZE_KEYS) <= set(i)


def test_risk_is_monotonic_in_edge_and_negative_edge_is_zero():
    ideas = _mixed_book()
    sizing.size_book(ideas, **P)
    risks = [i["risk_pct"] for i in ideas]
    # Ideas are in descending-edge order → risk% is non-increasing (ties allowed at the cap).
    assert risks == sorted(risks, reverse=True)
    # The negative-edge idea (win 47%, R 1.2 → Kelly < 0) is sized to zero.
    assert ideas[-1]["risk_pct"] == 0.0 and ideas[-1]["alloc_pct"] == 0.0


def test_allocation_follows_stop_distance_for_equal_edge():
    # Same edge, different stop distance → the tighter stop earns the larger position.
    ideas = [
        {"est_win_pct": 60, "r_multiple": 2.0, "entry": 100, "stop": 90},   # 10% stop
        {"est_win_pct": 60, "r_multiple": 2.0, "entry": 100, "stop": 85},   # 15% stop
    ]
    sizing.size_book(ideas, **P)
    assert ideas[0]["risk_pct"] == ideas[1]["risk_pct"]      # identical edge → identical risk
    assert ideas[0]["alloc_pct"] > ideas[1]["alloc_pct"]     # tighter stop → bigger position


def test_more_ideas_shrink_each_position():
    # A crowded book of strong ideas must diversify: per-trade risk drops to keep the book budget.
    strong = {"est_win_pct": 62, "r_multiple": 2.2, "entry": 100, "stop": 96, "conviction": 3}
    small = sizing.size_book([dict(strong) for _ in range(2)], **P)
    big = sizing.size_book([dict(strong) for _ in range(12)], **P)
    assert big["total_risk_pct"] <= 6.0 + 1e-9 and small["total_risk_pct"] <= 6.0 + 1e-9
    # Each of 12 identical ideas risks less than each of 2 identical ideas.
    per_small = small["total_risk_pct"] / small["sized"]
    per_big = big["total_risk_pct"] / big["sized"]
    assert per_big < per_small


# ── size_book: field aliases (daily-candidate shape) ────────────────


def test_daily_candidate_shape_via_aliases():
    # Daily ideas carry price/stop_price/risk_reward/avg_win_rate/stop_pct — sized via aliases.
    daily = [
        {"avg_win_rate": 58, "risk_reward": 2.2, "price": 200.0, "stop_price": 184.0, "stop_pct": 0.08, "conviction": 4},
        {"avg_win_rate": 50, "risk_reward": 1.4, "price": 80.0, "stop_price": 76.0, "stop_pct": 0.05, "conviction": 1},
    ]
    summ = sizing.size_book(daily, **P)
    assert summ["sized"] == 2 and summ["total_risk_pct"] <= 6.0 + 1e-9
    for c in daily:
        assert c["risk_pct"] > 0 and c["alloc_pct"] > 0
    # stop_pct is used directly: allocation ≈ risk / stop_pct (before caps/normalization here neither binds).
    assert daily[0]["alloc_pct"] == pytest.approx(daily[0]["risk_pct"] / 0.08, rel=0.02)


def test_reward_risk_derived_from_geometry_when_not_stored():
    # No r_multiple/risk_reward → derived from entry/stop/target (long).
    ideas = [{"est_win_pct": 60, "entry": 100, "stop": 90, "target": 130}]   # risk 10, reward 30 → b=3
    sizing.size_book(ideas, **P)
    assert ideas[0]["risk_pct"] > 0     # positive edge recognized from geometry


def test_short_geometry_reward_risk():
    ideas = [{"est_win_pct": 60, "side": "short", "entry": 100, "stop": 105, "target": 90}]  # risk5 reward10 b=2
    sizing.size_book(ideas, **P)
    assert ideas[0]["risk_pct"] > 0


# ── size_book: graceful fallback (no measured edge) ─────────────────


def test_fallback_fixed_fractional_when_no_win_prob():
    ideas = [
        {"entry": 100, "stop": 97, "conviction": 3},   # no win prob → fallback, high conviction
        {"entry": 100, "stop": 97, "conviction": 1},   # no win prob → fallback, low conviction
    ]
    sizing.size_book(ideas, **P)
    for i in ideas:
        assert i["kelly_full"] is None
        assert "fixed-fractional" in i["size_note"]
        assert 0.0 < i["risk_pct"] <= 1.5 + 1e-9
    # Higher conviction gets at least as much risk as lower conviction.
    assert ideas[0]["risk_pct"] >= ideas[1]["risk_pct"]


# ── sizing_params reflects config ───────────────────────────────────


def test_sizing_params_shape():
    p = sizing.sizing_params()
    assert set(p) >= {"enabled", "kelly_fraction", "max_position_pct",
                      "risk_per_trade_pct", "max_book_risk_pct"}
