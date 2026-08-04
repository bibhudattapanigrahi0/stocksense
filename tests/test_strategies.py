"""Strategy library integrity — every StrategyDef must be well-formed and runnable."""

import numpy as np
import pandas as pd
import pytest

from pipeline_core.backtest.engine import simulate
from pipeline_core.backtest.strategies import STRATEGIES, get_strategy


def _synthetic_ohlcv(n: int = 600, seed: int = 42) -> pd.DataFrame:
    """Trending-with-noise OHLCV long enough to warm up every indicator."""
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    idx = pd.date_range("2022-01-01", periods=n, freq="B")
    openp = close * (1 + rng.normal(0, 0.008, n))
    high = np.maximum.reduce([close * (1 + np.abs(rng.normal(0, 0.01, n))), openp, close])
    low = np.minimum.reduce([close * (1 - np.abs(rng.normal(0, 0.01, n))), openp, close])
    vol = np.abs(rng.normal(1e6, 3e5, n))
    return pd.DataFrame({"Open": openp, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


def test_library_size_and_unique_ids():
    assert len(STRATEGIES) >= 35
    ids = [s.id for s in STRATEGIES]
    assert len(ids) == len(set(ids)), "duplicate strategy ids"


def test_every_strategy_has_metadata():
    for s in STRATEGIES:
        assert s.id and s.name and s.category and s.description
        assert get_strategy(s.id) is s


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.id)
def test_signals_are_boolean_and_aligned(strategy):
    df = _synthetic_ohlcv()
    entry = strategy.entry(df)
    exit_ = strategy.exit(df)
    assert entry.dtype == bool and exit_.dtype == bool
    assert len(entry) == len(df) and len(exit_) == len(df)
    assert entry.index.equals(df.index)


def test_accumulation_family_present_and_classified():
    """The institutional-accumulation family exists, is categorized, and lands in the
    horizon buckets the Trade Ideas board expects (long / medium / medium)."""
    from pipeline_core.backtest.runner import strategy_horizon

    expected = {
        "institutional-accumulation": "long",
        "accumulation-breakout": "medium",
        "money-flow-thrust": "medium",
    }
    for sid, horizon in expected.items():
        s = get_strategy(sid)
        assert s.category == "Accumulation"
        assert strategy_horizon(s) == horizon


def test_inst13f_strategy_inert_without_data_fires_with():
    """The SEC-13F strategy is inert unless runner.py has merged the institutional columns
    (so non-US / unmapped names never crash), and fires on a fresh accumulating filing."""
    s = get_strategy("institutional-13f-accumulation")
    df = _synthetic_ohlcv()
    assert int(s.entry(df).sum()) == 0 and int(s.exit(df).sum()) == 0   # no columns → inert

    idx = pd.date_range("2015-01-01", periods=400, freq="B")
    up = pd.Series(np.linspace(50, 200, 400), index=idx)                 # clean uptrend > 200-DMA
    d = pd.DataFrame({"Open": up, "High": up + 1, "Low": up - 1, "Close": up, "Volume": 1e6}, index=idx)
    d["inst13f_shares"] = 1000.0
    d.iloc[300:, d.columns.get_loc("inst13f_shares")] = 1150.0           # +15% at a new filing
    d["inst13f_qoq"] = np.nan
    d.iloc[300:, d.columns.get_loc("inst13f_qoq")] = 15.0
    assert int(s.entry(d).sum()) == 1                                    # one entry, on the filing day


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.id)
def test_simulation_produces_sane_trades(strategy):
    """Runs without error; any trades have coherent dates and finite returns."""
    df = _synthetic_ohlcv()
    res = simulate("SYN", df, strategy)
    for t in res.trades:
        assert t.entry_date <= t.exit_date
        assert np.isfinite(t.ret_pct)
        assert t.holding_days >= 0
        assert t.exit_reason in {"signal", "stop", "trailing", "target", "time", "end_of_data"}
