"""My Hold Basket — hold-mode selection (staying power) + the material-change rebalance advisor. Offline.

The advisor is the anti-churn core: a still-valid holding is HELD (never churned for slipping a rank);
changes fire only on a material event (stop/target hit, thesis broke, or a much-higher-conviction name)."""

from pipeline_core.backtest import basket


# ── Fixtures ────────────────────────────────────────────────────────

def _cand(ticker, horizon="medium", conviction=3, rank_score=None, **over):
    c = {
        "ticker": ticker, "horizon": horizon, "horizon_label": horizon.title() + " term",
        "price": 100.0, "stop_pct": 0.06, "stop_price": 94.0, "target_pct": 0.12, "target_price": 112.0,
        "risk_reward": 2.0, "conviction": conviction, "eligible_conviction": conviction,
        "avg_win_rate": 55.0, "rank_score": rank_score if rank_score is not None else float(conviction),
        "sector": None, "zone": "in_zone", "is_new": False,
    }
    c.update(over)
    return c


def _board(by_horizon, regime=None):
    horizons = ["short", "medium", "long"]
    buckets = {h: {"horizon": h, "label": h.title() + " term", "candidates": by_horizon.get(h, [])}
               for h in horizons}
    return {"market": "IN", "horizons": horizons, "buckets": buckets, "regime": regime}


# ── Hold-mode selection ─────────────────────────────────────────────

def test_hold_mode_applies_conviction_floor():
    board = _board({"short": [_cand("LOW", conviction=1), _cand("OK", conviction=3)]})
    daily = basket.build_basket(board, clusters={})
    hold = basket.build_basket(board, clusters={}, hold_mode=True)
    daily_t = {it["ticker"] for it in daily["items"]}
    hold_t = {it["ticker"] for it in hold["items"]}
    assert "LOW" in daily_t                                  # daily keeps the conviction-1 name
    assert "LOW" not in hold_t and "OK" in hold_t            # hold book drops below the conviction floor
    assert hold["hold_mode"] is True


def test_hold_mode_tilts_to_longer_horizon():
    # Equal conviction short vs long; the long-horizon tilt should rank the long name ahead.
    board = _board({"short": [_cand("SH", horizon="short", conviction=3)],
                    "long": [_cand("LO", horizon="long", conviction=3)]})
    hold = basket.build_basket(board, clusters={}, hold_mode=True)
    # both may be selected, but the long name scores higher (×1.2 vs ×0.8)
    by = {it["ticker"]: it["basket_score"] for it in hold["items"]}
    assert by["LO"] > by["SH"]


def test_hold_mode_is_stickier_than_daily(monkeypatch):
    # A committed name (Y) vs a modestly-better challenger (X, +13% score). The daily margin (.10) would
    # swap to X; the hold margin (.30) keeps the incumbent Y. Size pinned to 1 so it must choose one.
    monkeypatch.setattr(basket.settings, "hold_size_min", 1)
    monkeypatch.setattr(basket.settings, "hold_size_max", 1)
    board = _board({"medium": [_cand("X", conviction=3, rank_score=3.4),
                               _cand("Y", conviction=3, rank_score=3.0)]},
                   regime={"state": "neutral", "tilt": 0.0})
    prev = {"items": [{"ticker": "Y", "horizon": "medium"}]}
    hold = basket.build_basket(board, clusters={}, prev_basket=prev, hold_mode=True)
    assert [it["ticker"] for it in hold["items"]] == ["Y"]   # wide hysteresis keeps the incumbent


# ── Material-change advisor ─────────────────────────────────────────

def _held(ticker, weight=0.2, entry=100.0, current=105.0, zone="in_zone", conviction=3):
    return {"ticker": ticker, "weight": weight, "entry_price": entry,
            "current_price": current, "zone": zone, "conviction": conviction}


def _fresh(ticker, conviction=3, alloc=20.0, score=0.8):
    return {"ticker": ticker, "conviction": conviction, "alloc_pct": alloc, "basket_score": score}


def test_holds_a_valid_name_that_is_not_top():
    # A holding that's still valid but no longer today's #1 must be HELD (no churn), and nothing material.
    held = [_held("A"), _held("B")]
    fresh = [_fresh("A"), _fresh("B")]                       # both still on the fresh board
    plan = basket.hold_rebalance_plan(held, fresh, target_size=2)
    assert plan["material"] is False
    assert {a["ticker"]: a["action"] for a in plan["actions"]} == {"A": "HOLD", "B": "HOLD"}


def test_exit_on_stop_target_and_thesis_break():
    held = [_held("STOP", zone="below_stop", current=93.0),
            _held("TGT", zone="at_target", current=113.0),
            _held("GONE", conviction=None),                 # no live signal on the board
            _held("WEAK", conviction=1)]                    # conviction below the floor (2)
    fresh = []                                              # nothing fresh to add
    plan = basket.hold_rebalance_plan(held, fresh, target_size=4, min_conviction=2)
    act = {a["ticker"]: a for a in plan["actions"]}
    assert all(act[t]["action"] == "EXIT" for t in ("STOP", "TGT", "GONE", "WEAK"))
    assert "stop" in act["STOP"]["reason"] and "target" in act["TGT"]["reason"]
    assert "thesis" in act["GONE"]["reason"] and "conviction" in act["WEAK"]["reason"]
    assert plan["material"] is True


def test_add_refills_slot_freed_by_exit():
    held = [_held("A"), _held("B", zone="below_stop", current=93.0)]   # B exits
    fresh = [_fresh("A"), _fresh("C", conviction=3)]                   # C is a fresh non-held name
    plan = basket.hold_rebalance_plan(held, fresh, target_size=2)
    acts = {a["ticker"]: a["action"] for a in plan["actions"]}
    assert acts["B"] == "EXIT" and acts["A"] == "HOLD" and acts["C"] == "ADD"
    assert set(plan["target"]) == {"A", "C"}                # B replaced by C, A kept


def test_no_add_when_book_is_full_and_no_displacement():
    # Book full, all valid, and the best fresh name is NOT dramatically higher conviction → no change.
    held = [_held("A", conviction=3), _held("B", conviction=3)]
    fresh = [_fresh("A"), _fresh("B"), _fresh("C", conviction=3)]      # C only equal conviction
    plan = basket.hold_rebalance_plan(held, fresh, target_size=2, conv_gap=2)
    assert plan["material"] is False
    assert "C" not in {a["ticker"] for a in plan["actions"]}


def test_displacement_only_on_big_conviction_jump():
    held = [_held("A", conviction=2), _held("B", conviction=2)]
    fresh = [_fresh("STRONG", conviction=5)]                          # 5 ≥ weakest(2) + gap(2)
    plan = basket.hold_rebalance_plan(held, fresh, target_size=2, conv_gap=2)
    acts = {a["ticker"]: a["action"] for a in plan["actions"]}
    assert acts["STRONG"] == "ADD"
    assert (acts.get("A") == "EXIT") ^ (acts.get("B") == "EXIT")      # exactly one holding displaced
    assert plan["material"] is True and len(plan["target"]) == 2
