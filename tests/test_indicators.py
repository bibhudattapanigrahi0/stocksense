"""Indicator math vs known-reference behavior."""

import numpy as np
import pandas as pd

from pipeline_core.agents import indicators


def test_sma_basic():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = indicators.sma(s, 3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == 2.0
    assert result.iloc[4] == 4.0


def test_ema_first_value_is_first_observation():
    s = pd.Series([10.0, 12.0, 14.0])
    result = indicators.ema(s, 2)
    assert result.iloc[0] == 10.0
    # adjust=False recursion: ema[1] = 12*(2/3) + 10*(1/3)
    assert abs(result.iloc[1] - (12 * 2 / 3 + 10 / 3)) < 1e-9


def test_rsi_all_gains_is_100():
    close = pd.Series(np.arange(1.0, 30.0))  # monotonically rising
    result = indicators.rsi(close, 14)
    assert result.iloc[-1] == 100.0


def test_rsi_all_losses_is_0():
    close = pd.Series(np.arange(30.0, 1.0, -1.0))
    result = indicators.rsi(close, 14)
    assert result.iloc[-1] < 1.0


def test_rsi_reference_values():
    # Classic Wilder example direction check: mixed series lands mid-range
    close = pd.Series(
        [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
         45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    )
    result = indicators.rsi(close, 14)
    # Well-known first RSI print for this textbook series is ≈ 70.5
    assert 68.0 < result.iloc[-2] < 73.0


def test_macd_columns_and_shape():
    close = pd.Series(np.sin(np.arange(100) / 5) * 10 + 100)
    df = indicators.macd(close)
    assert list(df.columns) == ["macd", "histogram", "signal"]
    assert len(df) == 100
    # histogram identity
    assert abs(df["histogram"].iloc[-1] - (df["macd"].iloc[-1] - df["signal"].iloc[-1])) < 1e-9


def test_annualized_volatility():
    rng = np.random.default_rng(7)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)))
    vol = indicators.annualized_volatility_pct(close)
    assert 10.0 < vol < 22.0  # 1% daily std → ~15.9% annualized


def test_beta_identical_series_is_one(price_history):
    beta = indicators.beta_vs_benchmark(price_history["Close"], price_history["Close"])
    assert beta is not None
    assert abs(beta - 1.0) < 1e-9


def test_beta_insufficient_overlap_returns_none():
    a = pd.Series([1.0, 2.0, 3.0])
    b = pd.Series([1.0, 2.0, 3.0])
    assert indicators.beta_vs_benchmark(a, b) is None


# ── Expansion indicators ───────────────────────────────────────────


def test_roc_basic():
    s = pd.Series([100.0, 105.0, 110.0, 121.0])
    r = indicators.roc(s, 1)
    assert abs(r.iloc[1] - 5.0) < 1e-9   # 105/100 - 1
    assert abs(r.iloc[3] - 10.0) < 1e-9  # 121/110 - 1


def test_williams_r_bounds_and_extremes():
    close = pd.Series(np.arange(1.0, 30.0))  # rising → close hugs the high
    high = close + 0.5
    low = close - 0.5
    wr = indicators.williams_r(high, low, close, 14)
    assert (wr.dropna() <= 0.01).all() and (wr.dropna() >= -100.01).all()
    assert wr.iloc[-1] > -20  # at highs → near 0 (overbought)


def test_cci_sign_tracks_trend():
    up = pd.Series(np.arange(1.0, 40.0))
    c = indicators.cci(up + 0.5, up - 0.5, up, 20)
    assert c.iloc[-1] > 0  # persistent uptrend → positive CCI


def test_keltner_geometry():
    close = pd.Series(np.linspace(100, 120, 60))
    high, low = close + 1.0, close - 1.0
    kc = indicators.keltner_channels(high, low, close, 20, 20, 2.0)
    assert list(kc.columns) == ["mid", "upper", "lower"]
    last = kc.iloc[-1]
    assert last["lower"] < last["mid"] < last["upper"]
    # width above/below mid is symmetric (both = mult * ATR)
    assert abs((last["upper"] - last["mid"]) - (last["mid"] - last["lower"])) < 1e-9


def test_supertrend_direction():
    rise = pd.Series(np.linspace(100, 200, 120))
    st = indicators.supertrend(rise + 1, rise - 1, rise, 10, 3.0)
    assert bool(st["uptrend"].iloc[-1]) is True
    assert st["supertrend"].iloc[-1] < rise.iloc[-1]  # trend line trails below price
    fall = pd.Series(np.linspace(200, 100, 120))
    st2 = indicators.supertrend(fall + 1, fall - 1, fall, 10, 3.0)
    assert bool(st2["uptrend"].iloc[-1]) is False


def test_adx_directional_and_bounded():
    rise = pd.Series(np.linspace(100, 200, 120))
    a = indicators.adx(rise + 1, rise - 1, rise, 14)
    last = a.iloc[-1]
    assert last["plus_di"] > last["minus_di"]        # uptrend → +DI dominates
    assert 0 <= last["adx"] <= 100


def test_obv_accumulates_signed_volume():
    close = pd.Series([10.0, 11.0, 10.0, 12.0])
    vol = pd.Series([100.0, 200.0, 150.0, 300.0])
    o = indicators.obv(close, vol)
    # diffs: NaN,+,-,+ → signed vol 0,+200,-150,+300 → cumsum
    assert list(o) == [0.0, 200.0, 50.0, 350.0]


def test_ad_line_signs_volume_by_close_location():
    # Bar 0 closes on the high (mfm=+1 → +V), bar 1 on the low (mfm=-1 → -V),
    # bar 2 at the exact midpoint (mfm=0 → 0). A/D is the running sum.
    high = pd.Series([10.0, 12.0, 10.0])
    low = pd.Series([8.0, 10.0, 6.0])
    close = pd.Series([10.0, 10.0, 8.0])
    vol = pd.Series([100.0, 200.0, 300.0])
    adl = indicators.ad_line(high, low, close, vol)
    assert list(adl) == [100.0, -100.0, -100.0]


def test_cmf_bounded_and_saturates():
    n = 5
    # Every bar closes on its high → pure accumulation → CMF saturates at +1.
    accum = indicators.cmf(pd.Series([10.0] * n), pd.Series([8.0] * n),
                           pd.Series([10.0] * n), pd.Series([100.0] * n), 3)
    assert np.isnan(accum.iloc[1])                  # warmup (< length)
    assert abs(accum.iloc[-1] - 1.0) < 1e-9
    # Every bar closes on its low → pure distribution → CMF = -1, still bounded.
    distrib = indicators.cmf(pd.Series([10.0] * n), pd.Series([8.0] * n),
                             pd.Series([8.0] * n), pd.Series([100.0] * n), 3).dropna()
    assert distrib.between(-1, 1).all()
    assert abs(distrib.iloc[-1] + 1.0) < 1e-9


def test_mfi_saturates_and_bounded():
    vol = pd.Series([100.0] * 20)
    up = pd.Series(np.linspace(10, 30, 20))          # typical price rises every bar → all up days
    m_up = indicators.mfi(up + 1, up - 1, up, vol, 14)
    assert np.isnan(m_up.iloc[5])                    # warmup (< 14)
    assert abs(m_up.iloc[-1] - 100.0) < 1e-9         # money only flowing in → 100
    assert m_up.dropna().between(0, 100).all()
    down = pd.Series(np.linspace(30, 10, 20))        # all down days → money only flowing out → 0
    m_down = indicators.mfi(down + 1, down - 1, down, vol, 14)
    assert abs(m_down.iloc[-1] - 0.0) < 1e-9
