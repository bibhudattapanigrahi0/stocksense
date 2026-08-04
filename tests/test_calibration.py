"""Win-rate calibration — isotonic (PAV) advertised→realized mapping + reliability tables.
Pure, network-free (mocked ledger)."""

from __future__ import annotations

from pipeline_core import calibration


# ── PAV isotonic ────────────────────────────────────────────────────


def test_pav_is_monotone_and_mean_preserving():
    y = [0, 1, 0, 1, 1, 0, 1, 1, 1]
    f = calibration.pav_isotonic([float(v) for v in y], [1.0] * len(y))
    assert f == sorted(f)                       # non-decreasing
    assert abs(sum(f) - sum(y)) < 1e-9          # PAV preserves the total (weighted mean)


def test_pav_leaves_monotone_input_unchanged():
    y = [0.0, 0.0, 0.5, 1.0, 1.0]
    assert calibration.pav_isotonic(y, [1.0] * len(y)) == y


# ── fit_calibration ─────────────────────────────────────────────────


def test_fit_requires_min_samples():
    assert calibration.fit_calibration([(0.6, True)] * 5, min_samples=20) is None


def test_fit_curve_is_monotone_and_not_worse_in_sample():
    # Advertised 0.7 but coin-flip reality → curve should not increase, and Brier must not worsen.
    samples = [(0.7, i % 2 == 0) for i in range(40)]
    curve = calibration.fit_calibration(samples, min_samples=20)
    assert curve and curve["n"] == 40
    ys = [k[1] for k in curve["knots"]]
    assert ys == sorted(ys)                                  # isotonic → non-decreasing knots
    assert curve["brier_cal"] <= curve["brier_raw"] + 1e-9   # calibration never worsens in-sample


# ── apply_calibration ───────────────────────────────────────────────


def test_apply_without_curve_returns_input():
    assert calibration.apply_calibration(None, 58) == 58.0
    assert calibration.apply_calibration({"knots": []}, 58) == 58.0


def test_apply_is_bounded_and_monotone():
    samples = [(p / 100.0, (p / 100.0) > 0.5 and i % 3 != 0) for i, p in enumerate(range(30, 90))]
    curve = calibration.fit_calibration(samples, min_samples=20)
    vals = [calibration.apply_calibration(curve, p) for p in range(30, 90, 5)]
    assert all(0.0 <= v <= 100.0 for v in vals)
    assert vals == sorted(vals)                              # monotone in the advertised rate


# ── reliability_by_conviction ───────────────────────────────────────


def test_reliability_by_conviction():
    rows = {2: [3, 6], 4: [8, 10]}                           # 3/6 and 8/10 wins
    table = calibration.reliability_by_conviction(rows)
    assert table == [{"conviction": 2, "n": 6, "realized_win_pct": 50.0},
                     {"conviction": 4, "n": 10, "realized_win_pct": 80.0}]


# ── build_from_ledger (mocked ledger) ───────────────────────────────


def _closed(market, pred, won, conv=3):
    return {"closed": True, "market": market, "avg_win_rate": pred,
            "conviction": conv, "realized_return_pct": (2.0 if won else -1.0)}


def test_build_from_ledger_calibrates_optimistic_rate(monkeypatch):
    from pipeline_core.backtest import runner
    ledger = [_closed("IN", 70, i % 2 == 0, conv=3) for i in range(40)]   # advertise 70%, win 50%
    ledger += [_closed("US", 95, True) for _ in range(5)]                 # other market → ignored
    monkeypatch.setattr(runner, "_load_tracked", lambda: ledger)

    cal = calibration.build_from_ledger("IN", min_samples=20)
    assert cal["active"] and cal["decided"] == 40 and cal["closed"] == 40
    assert 40.0 <= calibration.apply_calibration(cal["curve"], 70) <= 60.0    # pulled toward reality
    assert cal["reliability_by_conviction"][0]["conviction"] == 3


def test_build_from_ledger_insufficient_is_inactive(monkeypatch):
    from pipeline_core.backtest import runner
    monkeypatch.setattr(runner, "_load_tracked", lambda: [_closed("IN", 70, True) for _ in range(5)])
    cal = calibration.build_from_ledger("IN", min_samples=20)
    assert cal["active"] is False and cal["curve"] is None
