"""Strategy discovery — grammar compilation, the engine's train/test entry-window gate, the
out-of-sample keep-gate (incl. the anti-overfit degradation guard), persistence, promotion, and
an end-to-end run. Network-free (synthetic OHLCV + monkeypatched loaders)."""

import numpy as np
import pandas as pd
import pytest

from pipeline_core.backtest import discovery as D
from pipeline_core.backtest import runner
from pipeline_core.backtest.engine import backtest_strategy, simulate


def frame(closes, freq="B", start="2015-01-01"):
    c = np.asarray(closes, float)
    n = len(c)
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": np.full(n, 1e6)}, index=idx)


def _noise(n, seed, drift=0.0004, vol=0.02):
    return 100 * np.cumprod(1 + np.random.default_rng(seed).normal(drift, vol, n))


def _trending(n, seed):                       # persistent drift, low noise → a real, repeatable edge
    return 100 * np.cumprod(1 + np.random.default_rng(seed).normal(0.0012, 0.014, n))


# ── Grammar ─────────────────────────────────────────────────────────


def test_grammar_all_families_compile_to_valid_signals():
    df = frame(_noise(600, 0))
    for kind, fam in D.ENTRY_FAMILIES.items():
        params = {p: opts[0] for p, opts in fam["params"].items()}
        spec = {"entry": {"kind": kind, "params": params},
                "risk": D._sample_risk(__import__("random").Random(1), fam["exit"] is None)}
        s = D.build_strategy(spec)
        assert s.id.startswith("disc-") and s.category and s.name
        e, x = s.entry(df), s.exit(df)
        assert e.dtype == bool and len(e) == len(df)
        assert x.dtype == bool and len(x) == len(df)


def test_spec_id_is_deterministic():
    spec = {"entry": {"kind": "ma_cross", "params": {"fast": 20, "slow": 50}},
            "risk": {"stop_loss_pct": 0.08, "trailing_stop_pct": None, "target_pct": None, "max_holding_days": 40}}
    assert D.build_strategy(spec).id == D.build_strategy(spec).id
    # order of keys doesn't change the hash (canonicalised)
    spec2 = {"risk": {"max_holding_days": 40, "target_pct": None, "trailing_stop_pct": None, "stop_loss_pct": 0.08},
             "entry": {"params": {"slow": 50, "fast": 20}, "kind": "ma_cross"}}
    assert D.build_strategy(spec).id == D.build_strategy(spec2).id


def test_sample_specs_unique_and_seed_reproducible():
    specs = D._sample_specs(60, seed=42)
    ids = [D._spec_hash(s) for s in specs]
    assert len(ids) == len(set(ids))                          # deduped
    assert [D._spec_hash(s) for s in D._sample_specs(8, seed=7)] == \
           [D._spec_hash(s) for s in D._sample_specs(8, seed=7)]
    # families filter is honoured
    only = D._sample_specs(20, seed=1, families=["rsi_reversion"])
    assert all(s["entry"]["kind"] == "rsi_reversion" for s in only)


# ── Engine train/test entry-window gate ─────────────────────────────


def test_entry_window_splits_without_leakage():
    df = frame(_noise(500, 1, drift=0.0005))
    strat = D.build_strategy({"entry": {"kind": "rsi_reversion", "params": {"n": 2, "th": 20, "exit_th": 65}},
                              "risk": {"max_holding_days": 20}})
    split = df.index[int(len(df) * 0.7)]
    full = backtest_strategy(strat, {"T": df}, None, 7.0)["metrics"]["trades"]
    is_n = backtest_strategy(strat, {"T": df}, None, 5.0, 25.0, None, split)["metrics"]["trades"]
    oos_n = backtest_strategy(strat, {"T": df}, None, 2.0, 25.0, split, None)["metrics"]["trades"]
    assert is_n > 0 and oos_n > 0
    assert abs((is_n + oos_n) - full) <= 2                    # split ≈ partition (±boundary straddle)


def test_entry_window_default_is_unchanged():
    df = frame(_noise(400, 2))
    strat = D.build_strategy({"entry": {"kind": "ma_cross", "params": {"fast": 20, "slow": 50}},
                              "risk": {"max_holding_days": 40}})
    a = simulate("T", df, strat)
    b = simulate("T", df, strat, 25.0, None, None)
    assert len(a.trades) == len(b.trades)                     # defaults = prior behaviour


# ── Keep-gate (incl. the anti-overfit degradation guard) ────────────


def _m(trades, exp, sharpe, pf):
    return {"trades": trades, "expectancy_pct": exp, "sharpe": sharpe, "profit_factor": pf}


def test_keep_gate_accepts_robust_and_rejects_overfit():
    good_is = _m(60, 1.2, 1.1, 1.6)
    good_oos = _m(30, 0.9, 0.8, 1.4)
    assert D._is_promising(good_is, good_oos) is True
    # in-sample-only winner that collapses out-of-sample → rejected by the degradation guard
    assert D._is_promising(good_is, _m(30, 0.05, 0.2, 1.05)) is False
    # too few out-of-sample trades → rejected by the significance floor
    assert D._is_promising(good_is, _m(8, 1.0, 1.0, 1.5)) is False
    # negative out-of-sample expectancy → rejected
    assert D._is_promising(good_is, _m(30, -0.1, 0.6, 1.3)) is False
    # weak in-sample Sharpe → never a leader
    assert D._is_promising(_m(60, 0.5, 0.3, 1.1), good_oos) is False


def test_pf_handles_inf_string():
    assert D._pf({"profit_factor": "inf"}) == float("inf")
    assert D._pf({"profit_factor": 1.4}) == 1.4
    assert D._pf({}) == 0.0


# ── Persistence + promotion + runner resolution ─────────────────────


@pytest.fixture
def disc_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_disc_file", lambda market: tmp_path / f"discovered_{market}.json")
    return tmp_path


def test_persistence_roundtrip(disc_tmp):
    spec = {"entry": {"kind": "ma_cross", "params": {"fast": 20, "slow": 50}}, "risk": {"max_holding_days": 40}}
    strat = D.build_strategy(spec)
    payload = {"as_of": "2026-01-01", "market": "IN", "kept": 1,
               "survivors": [D._survivor_row(spec, strat, _m(50, 1.0, 1.2, 1.5), _m(20, 0.8, 0.9, 1.3), 3.0)]}
    D._persist_discovered(payload, "IN")
    loaded = D.load_discovered("IN")
    assert loaded["kept"] == 1 and loaded["survivors"][0]["id"] == strat.id
    assert loaded["survivors"][0]["promoted"] is False


def test_promote_resolves_like_a_builtin(disc_tmp):
    spec = {"entry": {"kind": "supertrend_flip", "params": {"period": 10, "mult": 3}},
            "risk": {"trailing_stop_pct": 0.12, "max_holding_days": 40}}
    strat = D.build_strategy(spec)
    D._persist_discovered({"market": "IN", "kept": 1,
                           "survivors": [D._survivor_row(spec, strat, _m(50, 1, 1.2, 1.5), _m(20, 0.8, 0.9, 1.3), 3.0)]}, "IN")
    assert D.get_discovered_strategy(strat.id, "IN").id == strat.id
    assert D.promote_discovered(strat.id, "IN")["status"] == "promoted"
    assert [s.id for s in D.promoted_strategies("IN")] == [strat.id]
    # runner sees it: plan resolves + library tags it discovered
    assert runner._strategy_plan(strat.id, "IN")                       # non-empty risk plan
    assert any(x["id"] == strat.id and x["discovered"] for x in runner.strategy_library("IN"))
    # un-promote + delete
    assert D.promote_discovered(strat.id, "IN", promoted=False)["status"] == "unpromoted"
    assert D.promoted_strategies("IN") == []
    assert D.delete_discovered(strat.id, "IN")["status"] == "deleted"
    assert D.get_discovered_strategy(strat.id, "IN") is None


def test_promote_unknown_id_errors(disc_tmp):
    D._persist_discovered({"market": "IN", "survivors": []}, "IN")
    assert "error" in D.promote_discovered("disc-nope", "IN")
    assert "error" in D.delete_discovered("disc-nope", "IN")


# ── End-to-end run_discovery ────────────────────────────────────────


def _wire_universe(monkeypatch, disc_tmp, closes_fn, tickers="ABCDEF"):
    uni = {t: frame(closes_fn(900, i)) for i, t in enumerate(tickers)}
    monkeypatch.setattr(D, "get_index_tickers", lambda index, market: list(uni))

    async def fake_hist(tk, years, progress_callback=None, market="IN"):
        if progress_callback:
            progress_callback(len(tk), len(tk))
        return uni, {"loaded": len(uni)}

    async def fake_bench(sym, years, market="IN"):
        return uni[tickers[0]]

    monkeypatch.setattr(D, "load_universe_history", fake_hist)
    monkeypatch.setattr(D, "load_benchmark", fake_bench)
    return uni


@pytest.mark.asyncio
async def test_run_discovery_keeps_survivors_on_trending_universe(monkeypatch, disc_tmp):
    _wire_universe(monkeypatch, disc_tmp, _trending)
    res = await D.run_discovery("IN", n_candidates=120, seed=5, years=8, top_k=20)
    assert res["status"] == "completed" and res["tried"] == 120
    assert res["kept"] >= 1                                    # a real trend edge survives OOS
    cat = D.list_discovered("IN")
    top = cat["survivors"][0]
    assert top["is_metrics"]["sharpe"] > 0 and top["oos_metrics"]["sharpe"] > 0
    assert top["oos_metrics"]["trades"] >= D.MIN_TRADES_OOS
    assert top["promoted"] is False and "spec" in top
    assert cat["kept"] == res["kept"] and cat["validated_oos"] >= cat["kept"]


@pytest.mark.asyncio
async def test_run_discovery_keeps_nothing_on_noise(monkeypatch, disc_tmp):
    _wire_universe(monkeypatch, disc_tmp, _noise)
    res = await D.run_discovery("IN", n_candidates=120, seed=9, years=8, top_k=20)
    assert res["status"] == "completed"
    assert res["kept"] == 0                                    # honest gate: no edge in random walks


@pytest.mark.asyncio
async def test_run_discovery_preserves_promotions_across_reruns(monkeypatch, disc_tmp):
    _wire_universe(monkeypatch, disc_tmp, _trending)
    await D.run_discovery("IN", n_candidates=120, seed=5, years=8, top_k=20)
    sid = D.list_discovered("IN")["survivors"][0]["id"]
    D.promote_discovered(sid, "IN")
    await D.run_discovery("IN", n_candidates=120, seed=5, years=8, top_k=20)   # same seed → same survivors
    kept = {s["id"]: s for s in D.list_discovered("IN")["survivors"]}
    assert kept[sid]["promoted"] is True                      # promotion carried over


@pytest.fixture
def anyio_backend():
    return "asyncio"
