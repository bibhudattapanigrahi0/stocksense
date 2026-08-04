"""Learned idea fusion — L2 logistic regression + walk-forward OOS activation guard.
Pure, network-free (mocked ledger)."""

from __future__ import annotations

import numpy as np

from pipeline_core import fusion


# ── logistic core ───────────────────────────────────────────────────


def test_fit_logistic_learns_monotone_signal():
    # y depends on the first feature → prediction should rise with it.
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 2))
    y = (X[:, 0] + rng.normal(scale=0.3, size=200) > 0).astype(float)
    model = fusion.fit_logistic(X, y, l2=0.5)
    lo = fusion.predict(model, [[-2.0, 0.0]])[0]
    hi = fusion.predict(model, [[2.0, 0.0]])[0]
    assert hi > lo and 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0


def test_predict_is_bounded():
    model = fusion.fit_logistic([[0.0], [1.0]], [0.0, 1.0])
    ps = fusion.predict(model, [[-99.0], [0.0], [99.0]])
    assert all(0.0 <= p <= 1.0 for p in ps)


# ── build_idea_model (mocked ledger) ────────────────────────────────


def _closed(market, conv, wr, sharpe, won):
    return {"closed": True, "market": market, "conviction": conv, "avg_win_rate": wr,
            "avg_sharpe": sharpe, "realized_return_pct": (2.0 if won else -1.0)}


def test_model_activates_on_a_real_signal(monkeypatch):
    from pipeline_core.backtest import runner
    # conviction carries a genuine signal (high conv → win), interleaved in time so both the train
    # and the validation split see both classes.
    ledger = []
    for i in range(80):
        conv = 5 if i % 2 == 0 else 1
        won = (conv == 5) if i % 5 != 0 else (conv != 5)   # strong but noisy signal
        ledger.append(_closed("IN", conv, 55, 1.0, won))
    monkeypatch.setattr(runner, "_load_tracked", lambda: ledger)

    b = fusion.build_idea_model("IN", min_samples=30)
    assert b["active"] is True and b["improvement"] > 0 and b["model"] is not None
    hi = fusion.model_win_pct(b, {"conviction": 5, "avg_win_rate": 55, "avg_sharpe": 1.0})
    lo = fusion.model_win_pct(b, {"conviction": 1, "avg_win_rate": 55, "avg_sharpe": 1.0})
    assert hi > lo                                          # higher conviction → higher learned P(win)


def test_model_inactive_without_enough_data(monkeypatch):
    from pipeline_core.backtest import runner
    monkeypatch.setattr(runner, "_load_tracked", lambda: [_closed("IN", 3, 55, 1.0, True) for _ in range(10)])
    b = fusion.build_idea_model("IN", min_samples=30)
    assert b["active"] is False and b["model"] is None
    assert fusion.model_win_pct(b, {"conviction": 3, "avg_win_rate": 55, "avg_sharpe": 1.0}) is None


def test_public_view_drops_raw_weights(monkeypatch):
    from pipeline_core.backtest import runner
    ledger = [_closed("IN", (i % 4) + 1, 50 + (i % 4) * 5, 1.0, (i % 4) >= 2) for i in range(80)]
    monkeypatch.setattr(runner, "_load_tracked", lambda: ledger)
    pub = fusion.public_view(fusion.build_idea_model("IN", min_samples=30))
    assert "model" not in pub and "features" in pub and "active" in pub


def test_model_win_pct_none_when_features_missing():
    bundle = {"active": True, "model": {"w": [0.0], "b": 0.0, "mean": [0.0], "std": [1.0], "n": 1}}
    assert fusion.model_win_pct(bundle, {"avg_win_rate": None}) is None
