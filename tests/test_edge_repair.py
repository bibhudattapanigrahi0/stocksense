"""Trade-ideas edge-repair wave: ledger costs, the breakeven edge floor, calibrated ranking, and
cohort soft-demote. Each fix ships behind a config toggle whose OFF state must reproduce prior
behaviour exactly — those regression guarantees are asserted alongside the new behaviour."""
import json

import pytest

from pipeline_core.backtest import runner
from pipeline_core.config import settings


def _cand(**over):
    """A minimal ranking candidate: 6% stop / 10% target (breakeven 0.375) unless overridden."""
    base = dict(ticker="AAA", side="long", conviction=3, entry_price=100.0,
                stop_price=94.0, target_price=110.0, stop_pct=0.06, target_pct=0.10,
                avg_win_rate=60.0, avg_sharpe=1.0)
    base.update(over)
    return base


# ── Fix 1: cost-in-ledger ───────────────────────────────────────────

def test_closed_outcome_is_net_of_cost_and_keeps_gross():
    out = runner._closed_outcome(_cand(market="US"), "target", 106.0, "2026-08-01", 5)
    assert out["gross_return_pct"] == 6.0
    assert out["realized_return_pct"] == 5.9        # US 10 bps round trip
    assert out["cost_pct"] == 0.1 and out["_cost_adjusted"] is True


def test_crypto_short_pays_funding_per_day_held():
    # 35 bps spot + 4 days × 10 bps funding = 0.75%; short profits as price falls 100→92 (+8 gross)
    out = runner._closed_outcome(_cand(market="CRYPTO", side="short"), "target", 92.0, "2026-08-01", 4)
    assert out["gross_return_pct"] == 8.0 and out["realized_return_pct"] == 7.25


def test_ledger_costs_toggle_off_is_gross(monkeypatch):
    monkeypatch.setattr(settings, "ledger_costs_enabled", False)
    out = runner._closed_outcome(_cand(market="CRYPTO"), "target", 110.0, "2026-08-01", 3)
    assert out["realized_return_pct"] == out["gross_return_pct"] == 10.0
    assert out["cost_pct"] == 0.0


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TRACKED_FILE", tmp_path / "t.json")
    runner._save_tracked([
        {"ticker": "X", "market": "US", "side": "long", "closed": True,
         "realized_return_pct": 6.0, "bars_held": 5},
        {"ticker": "Y", "closed": False, "realized_return_pct": None},   # open → skipped
    ])
    assert runner.backfill_ledger_costs() == {"adjusted": 1, "total": 2}
    assert runner.backfill_ledger_costs()["adjusted"] == 0                # re-run is a no-op
    rec = next(t for t in runner._load_tracked() if t["ticker"] == "X")
    assert rec["gross_return_pct"] == 6.0 and rec["realized_return_pct"] == 5.9


# ── Fix 2: breakeven edge floor ─────────────────────────────────────

def test_breakeven_and_edge_floor():
    assert round(runner._breakeven_win_prob(_cand()), 3) == 0.375        # 6 / (6+10)
    assert runner._passes_edge_floor(_cand(avg_win_rate=60))             # clears breakeven+3pp
    assert not runner._passes_edge_floor(_cand(avg_win_rate=33))         # coin-flip → dropped
    assert runner._passes_edge_floor(_cand(stop_pct=None, target_pct=None,
                                           stop_price=None, target_price=None))  # no geometry → can't judge


def test_rank_pool_drops_below_edge_and_reports_cut():
    pool = [_cand(ticker="GOOD", avg_win_rate=65), _cand(ticker="BAD", avg_win_rate=30)]
    shown, quality_cut, edge_cut, _conv, _ = runner._rank_pool(pool, 10, None, None)
    assert [c["ticker"] for c in shown] == ["GOOD"]
    assert edge_cut == 1 and quality_cut == 0


def test_edge_floor_never_empties_a_bucket():
    pool = [_cand(ticker=t, avg_win_rate=25) for t in ("A", "B", "C", "D")]   # all below edge
    shown, _, edge_cut, _conv, _ = runner._rank_pool(pool, 10, None, None)
    assert 1 <= len(shown) <= settings.edge_floor_min_keep                     # least-bad kept, flagged
    assert all(c.get("below_edge") for c in shown) and edge_cut == len(pool)


def test_edge_floor_toggle_off_keeps_everything(monkeypatch):
    monkeypatch.setattr(settings, "idea_min_edge_margin_pp", -100.0)           # floor can't reject
    pool = [_cand(ticker="GOOD", avg_win_rate=65), _cand(ticker="BAD", avg_win_rate=30)]
    shown, _, edge_cut, _conv, _ = runner._rank_pool(pool, 10, None, None)
    assert {c["ticker"] for c in shown} == {"GOOD", "BAD"} and edge_cut == 0


# ── Zero-edge conviction floor (equity) ─────────────────────────────

def test_conv_floor_drops_zero_edge_equity_idea():
    pool = [_cand(ticker="GOOD", avg_win_rate=65, weighted_conviction=0.8),
            _cand(ticker="NOEDGE", avg_win_rate=52, weighted_conviction=0.0)]  # no strategy clears the floor
    shown, _q, _edge, conv_cut, _dd = runner._rank_pool(pool, 10, None, None, "US")
    assert [c["ticker"] for c in shown] == ["GOOD"] and conv_cut == 1

def test_conv_floor_can_empty_a_bucket():
    pool = [_cand(ticker=t, avg_win_rate=52, weighted_conviction=0.0) for t in ("A", "B", "C")]
    shown, _q, _edge, conv_cut, _dd = runner._rank_pool(pool, 10, None, None, "US")
    assert shown == [] and conv_cut == 3          # all zero-edge → honest empty bucket, NOT padded

def test_conv_floor_exempts_crypto():
    pool = [_cand(ticker="X", weighted_conviction=0.0)]
    shown, _q, _edge, conv_cut, _dd = runner._rank_pool(pool, 10, None, None, "CRYPTO")
    assert conv_cut == 0 and len(shown) == 1      # crypto trends — momentum is its edge, not gated

def test_conv_floor_toggle_off(monkeypatch):
    monkeypatch.setattr(settings, "min_weighted_conviction", 0.0)
    pool = [_cand(ticker="X", weighted_conviction=0.0)]
    shown, _q, _edge, conv_cut, _dd = runner._rank_pool(pool, 10, None, None, "US")
    assert conv_cut == 0 and len(shown) == 1

def test_conv_floor_keeps_idea_missing_the_field():
    pool = [_cand(ticker="X", avg_win_rate=65)]    # no weighted_conviction → can't judge → kept
    shown, _q, _edge, conv_cut, _dd = runner._rank_pool(pool, 10, None, None, "US")
    assert conv_cut == 0 and len(shown) == 1


# ── Fix 3: calibrated ranking ───────────────────────────────────────

def test_calibrated_win_pct_preferred_over_advertised():
    c = _cand(avg_win_rate=60, calibrated_win_pct=38)
    assert runner._rank_win_prob(c) == 0.38          # calibrated wins
    assert not runner._passes_edge_floor(c)          # 0.38 < 0.405 → dropped on the honest number


def test_rank_use_calibrated_toggle_off_uses_advertised(monkeypatch):
    monkeypatch.setattr(settings, "rank_use_calibrated", False)
    c = _cand(avg_win_rate=60, calibrated_win_pct=38)
    assert runner._rank_win_prob(c) == 0.60          # ignores calibration when off


# ── Fix 4: cohort soft-demote ───────────────────────────────────────

def test_cohort_demote_reason_matrix():
    assert runner._cohort_demote_reason("CRYPTO", "short") == "crypto daily"
    assert runner._cohort_demote_reason("CRYPTO", "medium") == "crypto daily + medium horizon"
    assert runner._cohort_demote_reason("US", "medium") == "medium horizon"
    assert runner._cohort_demote_reason("US", "short") is None


def test_cohort_haircut_lowers_win_prob():
    plain, demoted = _cand(avg_win_rate=60), _cand(avg_win_rate=60, demote_reason="crypto daily")
    assert runner._rank_win_prob(plain) == 0.60
    assert round(runner._rank_win_prob(demoted), 3) == 0.52     # 60% − 8pp haircut


def test_recompose_tags_and_sizes_a_demoted_bucket():
    bucket = {"horizon": "medium",
              "candidates": [_cand(ticker="BTC", avg_win_rate=72), _cand(ticker="ETH", avg_win_rate=70)]}
    runner._recompose_bucket(bucket, 0.0, 10, market="CRYPTO")
    assert all(c.get("demote_reason") for c in bucket["candidates"])          # kept, but flagged
    assert bucket["sizing"]["cohort_size_factor"] == settings.cohort_size_factor


def test_cohort_demote_toggle_off_leaves_bucket_untouched(monkeypatch):
    monkeypatch.setattr(settings, "cohort_demote_enabled", False)
    bucket = {"horizon": "medium", "candidates": [_cand(ticker="BTC", avg_win_rate=72)]}
    runner._recompose_bucket(bucket, 0.0, 10, market="CRYPTO")
    assert not bucket["candidates"][0].get("demote_reason")
    assert "cohort_size_factor" not in bucket["sizing"]
