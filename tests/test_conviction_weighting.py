"""Edge-weighted conviction — the board's primary rank key. Raw strategy-count conviction was an
actively harmful ranker for equities (closed-ledger Spearman IC vs return: US -0.31) because it summed
contradictory families: low-win-rate momentum votes inflated the count on extended stocks that then
failed. Each vote is now weighted by its backtest win-rate. Crypto is exempt; the toggle-off path
reproduces the raw-count ordering exactly."""
import pytest

from pipeline_core.backtest import runner
from pipeline_core.config import settings


# ── _conviction_edge_weight ─────────────────────────────────────────

def test_edge_weight_is_clipped_linear_from_floor():
    assert runner._conviction_edge_weight(45) == 0.0       # at the floor
    assert runner._conviction_edge_weight(30) == 0.0       # below the floor
    assert runner._conviction_edge_weight(100) == 1.0      # ceiling
    assert 0.30 < runner._conviction_edge_weight(63) < 0.35  # mean-reversion ~0.33
    assert runner._conviction_edge_weight(None) == 0.5     # unknown → neutral half-vote


def test_edge_weight_respects_configurable_floor(monkeypatch):
    monkeypatch.setattr(settings, "conviction_edge_floor_pct", 50.0)
    assert runner._conviction_edge_weight(50) == 0.0
    assert runner._conviction_edge_weight(75) == 0.5


# ── _weighted_conviction ────────────────────────────────────────────

def test_momentum_heavy_scores_below_clean_mean_reversion_at_equal_count():
    mr = [{"name": "Williams %R", "win_rate": 71}, {"name": "Connors RSI(2)", "win_rate": 64},
          {"name": "Bollinger Lower", "win_rate": 74}]
    mom = [{"name": "63-Day ROC", "win_rate": 43}, {"name": "Bollinger Upper", "win_rate": 42},
           {"name": "Williams %R", "win_rate": 71}]
    assert len(mr) == len(mom) == 3                                  # identical RAW conviction
    assert runner._weighted_conviction(mr) > 2 * runner._weighted_conviction(mom)  # but far apart weighted


def test_weighted_conviction_uses_wr_map_for_name_only_carried():
    wr_map = {"Williams %R": 71, "63-Day ROC": 43}
    named = runner._weighted_conviction(["Williams %R", "63-Day ROC"], wr_map)
    dicts = runner._weighted_conviction([{"name": "Williams %R", "win_rate": 71},
                                         {"name": "63-Day ROC", "win_rate": 43}])
    assert named == dicts                                            # same scale, fresh or carried


# ── _ranking_conviction (the base-score selector) ───────────────────

def _cand():
    return {"conviction": 5, "weighted_conviction": 1.7}

def test_equity_ranks_on_weighted_crypto_and_others_on_raw():
    assert runner._ranking_conviction(_cand(), "US") == 1.7         # equity → weighted
    assert runner._ranking_conviction(_cand(), "IN") == 1.7
    assert runner._ranking_conviction(_cand(), "CRYPTO") == 5       # crypto exempt → raw

def test_toggle_off_reproduces_raw_conviction(monkeypatch):
    monkeypatch.setattr(settings, "conviction_edge_weighting", False)
    assert runner._ranking_conviction(_cand(), "US") == 5           # off → raw (prior ordering)

def test_market_not_in_edge_list_uses_raw(monkeypatch):
    monkeypatch.setattr(settings, "conviction_edge_markets", ("US",))
    assert runner._ranking_conviction(_cand(), "IN") == 5           # IN not listed → raw

def test_missing_weighted_conviction_falls_back_to_raw():
    assert runner._ranking_conviction({"conviction": 4}, "US") == 4  # legacy candidate w/o the field
