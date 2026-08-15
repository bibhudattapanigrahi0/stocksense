"""Market-wide BTC regime filter for crypto strategies.

The crypto strategy library backtested with ~−90% drawdowns because every entry used a *per-coin*
regime proxy — in a crypto bear market all coins crash with BTC, so the book was one giant beta bet.
These tests cover the fix: a historical BTC risk-on/off series
(`market_regime.regime_series`) injected as a `MKT_RISK_ON` column onto every coin frame
(`market_regime.enrich_frames_with_market`), which the market-gated crypto strategies read so they sit
out crypto winters. All network-free (synthetic series). The per-coin fallback (no column) is asserted
to preserve the prior behaviour exactly.
"""

import numpy as np
import pandas as pd

from pipeline_core.backtest import strategies as S
from pipeline_core.backtest.strategies import strategies_for_market
from pipeline_core.services import market_regime as mr


def _series(values, start="2015-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(np.asarray(values, dtype=float), index=idx)


def _ohlc(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": 1e6},
        index=close.index,
    )


# ── regime_series ────────────────────────────────────────────────────

def test_regime_series_risk_on_in_uptrend_off_in_downtrend():
    close = _series(np.concatenate([np.linspace(100, 500, 300), np.linspace(500, 150, 250)]))
    s = mr.regime_series(close)
    assert s.dtype == bool
    assert len(s) == len(close)
    assert bool(s.iloc[260]) is True          # deep in the uptrend
    assert bool(s.iloc[520]) is False         # deep in the downtrend


def test_regime_series_empty_and_none():
    assert mr.regime_series(None) is None
    assert mr.regime_series(pd.Series(dtype=float)) is None


def test_regime_series_no_lookahead():
    # The regime at date i must not depend on any data after i: computing on the full series and on the
    # series truncated at i must agree at i (rolling MA + shift(1) are strictly backward-looking).
    rng = np.random.default_rng(0)
    close = _series(100 + np.cumsum(rng.normal(0.3, 2.0, 400)))
    full = mr.regime_series(close)
    for i in (250, 300, 380):
        trunc = mr.regime_series(close.iloc[: i + 1])
        assert bool(full.iloc[i]) == bool(trunc.iloc[i])


# ── enrich_frames_with_market ────────────────────────────────────────

def test_enrich_injects_aligned_column():
    close = _series(np.concatenate([np.linspace(100, 500, 300), np.linspace(500, 150, 250)]))
    # A coin trading over the benchmark's downtrend window should read risk-off there.
    coin = _ohlc(_series(np.linspace(10, 5, 200), start="2015-11-01"))
    frames = {"FOO": coin}
    mr.enrich_frames_with_market(frames, close)
    assert "MKT_RISK_ON" in coin.columns
    assert coin["MKT_RISK_ON"].dtype == bool
    assert bool((~coin["MKT_RISK_ON"]).any())          # some risk-off days land on this coin


def test_enrich_is_tz_safe_and_never_raises():
    close = _series(np.linspace(100, 400, 400))
    coin = _ohlc(_series(np.linspace(1, 2, 100), start="2015-06-01"))
    coin.index = coin.index.tz_localize("UTC")          # tz-aware coin frame
    frames = {"FOO": coin, "EMPTY": pd.DataFrame(), "NONE": None}
    mr.enrich_frames_with_market(frames, close)          # must not raise on empty/None frames
    assert "MKT_RISK_ON" in coin.columns
    assert len(coin["MKT_RISK_ON"]) == len(coin)


# ── strategy market-gating ───────────────────────────────────────────

def _uptrend_frame(n=260):
    return _ohlc(_series(np.linspace(100, 400, n), start="2022-01-01"))


def _downtrend_frame(n=260):
    return _ohlc(_series(np.linspace(400, 100, n), start="2022-01-01"))


def test_long_entry_blocked_when_market_risk_off():
    df = _uptrend_frame()
    on, off = df.copy(), df.copy()
    on["MKT_RISK_ON"], off["MKT_RISK_ON"] = True, False
    assert int(S._crypto_breakout_entry(off).sum()) == 0        # gated out in a market-wide bear
    assert int(S._crypto_breakout_entry(on).sum()) > 0          # fires when the market is risk-on


def test_short_entry_only_when_market_risk_off():
    df = _downtrend_frame()
    on, off = df.copy(), df.copy()
    on["MKT_RISK_ON"], off["MKT_RISK_ON"] = True, False
    assert int(S._crypto_breakdown_entry(on).sum()) == 0        # never short a risk-on market
    assert int(S._crypto_breakdown_entry(off).sum()) > 0        # fires in a market-wide bear


def test_regime_exit_goes_to_cash_when_market_flips_off():
    df = _uptrend_frame()
    off = df.copy()
    off["MKT_RISK_ON"] = False
    assert bool(S._crypto_regime_exit(off).all())              # every bar is an exit → flat book
    on = df.copy()
    on["MKT_RISK_ON"] = True
    # With the market risk-on the exit is just the structural 50-DMA break (rare in a clean uptrend).
    assert int(S._crypto_regime_exit(on).sum()) < len(df)


def test_helpers_fall_back_to_per_coin_proxy_without_column():
    # No injected column ⇒ the market gate must reduce EXACTLY to the old per-coin proxy, so the
    # pre-existing crypto behaviour (and its tests) are unchanged.
    df = _uptrend_frame()
    assert "MKT_RISK_ON" not in df.columns
    pd.testing.assert_series_equal(S._mkt_risk_on(df), S._crypto_regime_ok(df), check_names=False)
    pd.testing.assert_series_equal(S._mkt_risk_off(df), S._crypto_regime_off(df), check_names=False)


# ── new strategies wired into the crypto set ─────────────────────────

def test_new_regime_strategies_registered():
    ids = {s.id for s in strategies_for_market("CRYPTO")}
    assert "crypto-regime-trend" in ids


def test_new_trend_strategy_respects_market_gate():
    df = _uptrend_frame()
    off, on = df.copy(), df.copy()
    off["MKT_RISK_ON"], on["MKT_RISK_ON"] = False, True
    assert int(S._crypto_regime_trend_entry(off).sum()) == 0
    assert int(S._crypto_regime_trend_entry(on).sum()) > 0


# ── Speculative-short surfacing in a bearish tide (_crypto_idea_gate) ─────────────────

from pipeline_core.backtest.runner import _crypto_idea_gate   # noqa: E402
from pipeline_core.config import settings                     # noqa: E402


def _long(t, elig=1, **o):
    return {"ticker": t, "side": "long", "conviction": elig, "eligible_conviction": elig, **o}


def _short(t, conv=1, elig=0, sharpe=-0.6, dd=-100.0, **o):
    return {"ticker": t, "side": "short", "conviction": conv, "eligible_conviction": elig,
            "avg_sharpe": sharpe, "avg_max_drawdown": dd, **o}


def test_unproven_shorts_surface_only_in_bearish_tide():
    cands = [_long("BTC"), _short("HTX"), _short("PEPE")]
    bearish = _crypto_idea_gate([dict(c) for c in cands], tilt=-0.5)
    risk_on = _crypto_idea_gate([dict(c) for c in cands], tilt=0.3)
    spec_bear = [c for c in bearish if c.get("side") == "short"]
    spec_on = [c for c in risk_on if c.get("side") == "short"]
    assert len(spec_bear) == 2 and all(c["speculative"] for c in spec_bear)   # surfaced + flagged
    assert spec_on == []                                                       # dropped on a risk-on day


def test_speculative_shorts_capped_and_ranked_by_conviction():
    shorts = [_short(f"C{i}", conv=i) for i in range(1, 7)]   # conviction 1..6
    kept = _crypto_idea_gate([_long("BTC")] + shorts, tilt=-0.4)
    spec = [c for c in kept if c.get("side") == "short"]
    assert len(spec) == settings.crypto_shorts_max_speculative               # capped
    assert [c["conviction"] for c in spec] == sorted([c["conviction"] for c in spec], reverse=True)
    assert spec[0]["conviction"] == 6                                        # strongest agreement leads


def test_proven_short_kept_even_risk_on_and_not_speculative():
    proven = _short("EDGE", conv=3, elig=2, sharpe=0.9, dd=-40.0)   # cleared the risk gate
    kept = _crypto_idea_gate([_long("BTC"), proven], tilt=0.5)      # risk-ON tide
    got = [c for c in kept if c.get("side") == "short"]
    assert len(got) == 1 and got[0]["ticker"] == "EDGE"
    assert not got[0].get("speculative")


def test_gate_never_empties_the_long_side():
    # Every long gated (eligible_conviction 0, not carried) ⇒ they're still all kept.
    longs = [_long("A", elig=0), _long("B", elig=0)]
    kept = _crypto_idea_gate(longs, tilt=-0.5)
    assert {c["ticker"] for c in kept} == {"A", "B"}
