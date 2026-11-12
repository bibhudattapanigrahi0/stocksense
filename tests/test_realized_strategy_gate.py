"""Realized-edge STRATEGY gate — the measure→apply loop for strategies.

Every other edge-weight keys off BACKTEST win-rate, which is blind to a strategy that backtests well
but loses LIVE. This gate reads the closed ledger and soft-demotes strategies that lose money live by a
MATERIAL margin (realized profit factor < ceiling), shaving their effective win-rate before the edge-
weight ramp so their vote shrinks / zeroes. Profit factor (not win-rate, not a bare avg<0) is the
trigger, so a low-win / high-payoff momentum strategy is kept and a near-breakeven diversifier is
spared. Each behaviour is asserted alongside its toggle-off regression guarantee.
"""
import pytest

from pipeline_core.backtest import runner
from pipeline_core.backtest import idea_ranking as ir
from pipeline_core.config import settings


def _closed(strats, rets, market="US"):
    """Expand a strategy → [returns] spec into closed-ledger records (one per return), each naming the
    given strategies. Mirrors how the tracker stores a closed idea: a `strategies` list + realized %."""
    out = []
    for r in rets:
        out.append({"closed": True, "market": market, "realized_return_pct": r,
                    "strategies": [{"name": n} for n in strats]})
    return out


# A clean, numeric fixture — each strategy's live record is unambiguous.
LEDGER = (
    _closed(["LOSER"],     [-10, -10, -10, -10, 2])     # pf 0.05, win 20%  → demote
    + _closed(["MODLOSER"], [-5, -5, -5, 3, 3])         # pf 0.40, win 40%  → demote (losing by margin)
    + _closed(["WINNER"],   [10, 10, -5, 10])           # pf 6.00, win 75%  → keep
    + _closed(["HIGHPAY"],  [40, -5, -5, -5, -5])       # pf 2.00, win 20%  → keep (few big winners pay)
    + _closed(["BREAKEVEN"], [3, -3, 2, -3, 2, -2])     # pf 0.88, win 50%  → keep (materiality margin)
    + _closed(["ALLWIN"],   [5, 5, 5, 5])               # pf None (no losses) → keep
)


# ── strategy_realized_edge (per-strategy attribution) ───────────────

def test_realized_edge_attributes_each_strategy():
    edge = ir.strategy_realized_edge(LEDGER)
    assert edge["LOSER"] == {"n": 5, "win_rate": 20.0, "avg_return": -7.6, "profit_factor": 0.05}
    assert edge["WINNER"]["profit_factor"] == 6.0 and edge["WINNER"]["win_rate"] == 75.0
    assert edge["ALLWIN"]["profit_factor"] is None            # no losing trade → PF undefined


def test_realized_edge_counts_a_strategy_once_per_idea_it_backs():
    # An idea surfaced by two strategies attributes its return to BOTH.
    led = [{"closed": True, "realized_return_pct": 8.0,
            "strategies": [{"name": "A"}, {"name": "B"}]}]
    edge = ir.strategy_realized_edge(led)
    assert edge["A"]["n"] == 1 and edge["B"]["n"] == 1 and edge["A"]["avg_return"] == 8.0


# ── strategy_demote_map (the trigger) ───────────────────────────────

def test_demote_map_triggers_on_profit_factor_margin():
    dm = ir.strategy_demote_map(LEDGER, min_samples=4, pf_ceiling=0.8)
    assert set(dm) == {"LOSER", "MODLOSER"}                   # only the material losers
    # NOT demoted: a winner, a low-win/high-payoff momentum strat, a near-breakeven diversifier, all-wins.
    for keep in ("WINNER", "HIGHPAY", "BREAKEVEN", "ALLWIN"):
        assert keep not in dm


def test_demote_map_respects_min_samples():
    # LOSER has only 5 trades: below a min_samples of 12 nothing is judged.
    assert ir.strategy_demote_map(LEDGER, min_samples=12, pf_ceiling=0.8) == {}


def test_demote_map_haircut_value_comes_from_config(monkeypatch):
    monkeypatch.setattr(settings, "realized_gate_haircut_pp", 17.0)
    dm = ir.strategy_demote_map(LEDGER, min_samples=4, pf_ceiling=0.8)
    assert dm["LOSER"] == 17.0                                # name → the configured pp haircut


def test_demote_map_empty_when_gate_off(monkeypatch):
    monkeypatch.setattr(settings, "realized_strategy_gate_enabled", False)
    assert ir.strategy_demote_map(LEDGER, min_samples=4) == {}   # off ⇒ empty ⇒ prior board


# ── _weighted_conviction haircut (the equity lever) ─────────────────

def test_haircut_shrinks_a_demoted_strategys_vote():
    strat = [{"name": "LOSER", "win_rate": 60}]               # 60% backtest → a positive vote normally
    plain = runner._weighted_conviction(strat)
    hair = runner._weighted_conviction(strat, None, {"LOSER": 20})   # 60−20=40 < 45 floor → vote 0
    assert plain > 0 and hair == 0.0


def test_weighted_conviction_parity_when_map_empty_or_none():
    strat = [{"name": "LOSER", "win_rate": 60}, {"name": "WINNER", "win_rate": 80}]
    base = runner._weighted_conviction(strat)
    assert runner._weighted_conviction(strat, None, None) == base
    assert runner._weighted_conviction(strat, None, {}) == base      # empty map ⇒ exact prior value


def _cand(**over):
    base = dict(ticker="AAA", side="long", conviction=3, entry_price=100.0,
                stop_price=94.0, target_price=110.0, stop_pct=0.06, target_pct=0.10,
                avg_win_rate=60.0, avg_sharpe=1.0)
    base.update(over)
    return base


def test_haircut_zeroes_a_sole_loser_so_the_conv_floor_drops_it():
    dm = {"LOSER": 20}
    loser_strats = [{"name": "LOSER", "win_rate": 60}]
    good_strats = [{"name": "WINNER", "win_rate": 70}]
    pool = [
        _cand(ticker="GOOD", weighted_conviction=runner._weighted_conviction(good_strats, None, dm)),
        _cand(ticker="SOLE_LOSER", weighted_conviction=runner._weighted_conviction(loser_strats, None, dm)),
    ]
    shown, _q, _edge, conv_cut, _dd = runner._rank_pool(pool, 10, None, None, "US")
    assert [c["ticker"] for c in shown] == ["GOOD"] and conv_cut == 1   # sole-loser dropped at the floor


def test_a_loser_mixed_with_winners_survives_lower():
    # An idea backed by a loser AND strong winners keeps a positive weighted conviction (soft demote).
    mixed = [{"name": "LOSER", "win_rate": 60}, {"name": "W1", "win_rate": 80}, {"name": "W2", "win_rate": 85}]
    wc = runner._weighted_conviction(mixed, None, {"LOSER": 20})
    assert wc > settings.min_weighted_conviction               # survives the floor, just ranks lower


# ── the UI flag ─────────────────────────────────────────────────────

def test_tag_flags_a_candidate_backed_by_a_demoted_strategy():
    cand = {"strategies": [{"name": "LOSER"}, {"name": "WINNER"}]}
    runner._tag_realized_demote(cand, {"LOSER": 20})
    assert cand["realized_demoted"] is True and "LOSER" in cand["realized_demote_reason"]


def test_tag_is_silent_for_a_clean_idea_or_empty_map():
    clean = {"strategies": [{"name": "WINNER"}]}
    runner._tag_realized_demote(clean, {"LOSER": 20})
    assert "realized_demoted" not in clean
    also = {"strategies": [{"name": "LOSER"}]}
    runner._tag_realized_demote(also, {})                      # empty map ⇒ no flag
    assert "realized_demoted" not in also


# ── crypto: drop a live-losing speculative short ────────────────────

def _spec_short(**over):
    base = dict(ticker="PEPE-USD", side="short", carried=False, eligible_conviction=0,
                conviction=2, avg_sharpe=0.1, avg_max_drawdown=-30.0,
                strategies=[{"name": "SHORTBAD"}])
    base.update(over)
    return base


def test_crypto_gate_surfaces_an_unproven_short_without_the_gate():
    kept = runner._crypto_idea_gate([_spec_short()], tilt=-0.5, demote_map=None)
    assert any(c.get("speculative") and c.get("side") == "short" for c in kept)   # bearish tide → surfaced


def test_crypto_gate_drops_a_proven_live_losing_short():
    kept = runner._crypto_idea_gate([_spec_short()], tilt=-0.5, demote_map={"SHORTBAD": 20})
    assert not any(c.get("side") == "short" for c in kept)     # proven loser → never surfaced, even speculatively


def test_crypto_gate_keeps_a_long_untouched_by_the_short_filter():
    longc = _cand(ticker="BTC-USD", eligible_conviction=1)
    kept = runner._crypto_idea_gate([longc, _spec_short()], tilt=-0.5, demote_map={"SHORTBAD": 20})
    assert any(c.get("side", "long") != "short" for c in kept)


# ── the market-scoped map loader ────────────────────────────────────

def test_demote_map_loader_scopes_by_market(monkeypatch):
    led = (_closed(["SHORTBAD"], [-9] * 13, market="CRYPTO")     # a crypto-only live loser
           + _closed(["WINNER"], [8] * 13, market="US"))
    monkeypatch.setattr(runner, "_load_tracked", lambda: led)
    assert "SHORTBAD" in runner._strategy_demote_map("CRYPTO")   # judged in its own market
    assert runner._strategy_demote_map("US") == {}               # not attributed to US


def test_demote_map_loader_respects_market_scope(monkeypatch):
    led = _closed(["SHORTBAD"], [-9] * 13, market="CRYPTO")
    monkeypatch.setattr(runner, "_load_tracked", lambda: led)
    monkeypatch.setattr(settings, "realized_gate_markets", ("US", "IN"))
    assert runner._strategy_demote_map("CRYPTO") == {}           # CRYPTO out of scope ⇒ no map


# ── strategy_rescue_map (the LIFT / re-admit side) ──────────────────

def test_rescue_map_admits_proven_live_winners():
    rm = ir.strategy_rescue_map(LEDGER, min_samples=4, pf_floor=1.5)
    assert rm == {"WINNER", "HIGHPAY"}                           # PF 6.0 and 2.0 clear 1.5
    for out in ("LOSER", "MODLOSER", "BREAKEVEN", "ALLWIN"):
        assert out not in rm                                     # losers, breakeven, undefined-PF stay out


def test_rescue_respects_min_samples():
    assert ir.strategy_rescue_map(LEDGER, min_samples=12, pf_floor=1.5) == set()


def test_rescue_empty_when_off(monkeypatch):
    monkeypatch.setattr(settings, "realized_rescue_enabled", False)
    assert ir.strategy_rescue_map(LEDGER, min_samples=4) == set()


def test_demote_and_rescue_are_disjoint():
    # PF < 0.8 (demote) and PF >= 1.5 (rescue) cannot overlap — a strategy is never both.
    dm = ir.strategy_demote_map(LEDGER, min_samples=4)
    rm = ir.strategy_rescue_map(LEDGER, min_samples=4)
    assert not (set(dm) & rm)


# ── crypto: re-admit a gated-but-live-winning strategy to eligibility ──

def _gated_crypto_latest(name="EMA 9/21 Crossover"):
    """A crypto long strategy that FAILS the backtest risk gate (dd −99%, Sharpe 0.1) yet fires a buy —
    exactly the momentum profile the ledger shows winning big live. id resolves to a real long StrategyDef."""
    return {"run_at": "2026-06-01T00:00:00+00:00", "market": "CRYPTO", "index": "Top 30",
            "benchmark_label": "Bitcoin", "years": 2, "disclaimer": "",
            "strategies": [{"id": "crypto-momentum-regime", "name": name, "category": "Momentum",
                "metrics": {"win_rate": 30.0, "sharpe": 0.1, "expectancy_pct": 1.0, "max_drawdown_pct": -99.0},
                "signals": {"buy": [{"ticker": "BTC-USD", "price": 100.0, "signal_date": "2026-06-01"}],
                            "sell": [], "holding": []}}]}


def _winning_ledger(name="EMA 9/21 Crossover"):
    """13 closed CRYPTO trades for `name`, PF ~22 → a proven live winner (clears min_samples 12 + pf 1.5)."""
    return [{"closed": True, "market": "CRYPTO", "realized_return_pct": r, "strategies": [{"name": name}]}
            for r in [20.0] * 10 + [-3.0] * 3]


def _btc_eligible_conviction(ideas):
    for h in ideas["horizons"]:
        for c in ideas["buckets"][h]["candidates"]:
            if c["ticker"] == "BTC-USD" and c.get("side", "long") != "short":
                return c.get("eligible_conviction")
    return None


def test_rescue_readmits_a_gated_live_winner_to_crypto_eligibility(monkeypatch):
    monkeypatch.setattr(runner, "_load_tracked", lambda: _winning_ledger())
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _gated_crypto_latest())
    ideas = runner.horizon_ideas(market="CRYPTO")
    assert _btc_eligible_conviction(ideas) == 1        # risk gate failed, but proven live ⇒ re-admitted


def test_rescue_off_leaves_the_gated_winner_ineligible(monkeypatch):
    monkeypatch.setattr(runner, "_load_tracked", lambda: _winning_ledger())
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _gated_crypto_latest())
    monkeypatch.setattr(settings, "realized_rescue_enabled", False)
    ideas = runner.horizon_ideas(market="CRYPTO")
    assert _btc_eligible_conviction(ideas) == 0        # risk gate stands ⇒ not counted (prior behaviour)
