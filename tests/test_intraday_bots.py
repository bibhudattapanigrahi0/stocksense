"""Intraday crypto bots — timeframe data, signal bots (fire / no-fire), and the scanner.
Network-free (synthetic OHLCV, mocked klines)."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from pipeline_core.agents import indicators as ind
from pipeline_core.bots import scan as scan_mod
from pipeline_core.bots import signals  # noqa: F401 — registers the bots
from pipeline_core.bots.base import get_bot, list_bots
from pipeline_core.services import crypto_data as cd


def frame(closes, vols=None, freq="15min", highs=None, lows=None):
    closes = np.asarray(closes, float)
    n = len(closes)
    idx = pd.date_range("2026-07-27", periods=n, freq=freq)
    return pd.DataFrame({
        "Open": closes,
        "High": highs if highs is not None else closes * 1.005,
        "Low": lows if lows is not None else closes * 0.995,
        "Close": closes,
        "Volume": vols if vols is not None else np.full(n, 1e6),
    }, index=idx)


# ── Timeframe data ──────────────────────────────────────────────────


def test_timeframes_registry():
    assert set(cd.TIMEFRAMES) >= {"5m", "10m", "15m", "30m", "1h", "4h"}
    assert cd.TIMEFRAMES["10m"] == ("5m", "10min")   # 10m resampled from 5m


def test_get_klines_resamples_10m(monkeypatch):
    five = frame(np.linspace(100, 110, 120), freq="5min")
    monkeypatch.setattr(cd, "_binance_klines", lambda sym, interval, bars: five)
    monkeypatch.setattr(cd.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(cd.cache, "set", lambda *_a, **_k: None)
    df = cd.get_klines("X", "10m", 30)
    assert (df.index[1] - df.index[0]) == pd.Timedelta("10min")   # resampled to 10-minute bars
    assert len(df) == 30


def test_get_klines_unknown_timeframe():
    with pytest.raises(ValueError):
        cd.get_klines("X", "7m", 100)


def test_vwap_within_window_range():
    df = frame(np.linspace(100, 120, 60))
    v = ind.vwap(df["High"], df["Low"], df["Close"], df["Volume"], 20)
    # VWAP is a 20-bar average — bounded by the window's low/high, not the current bar.
    assert float(df["Low"].iloc[-20:].min()) <= float(v.iloc[-1]) <= float(df["High"].iloc[-20:].max())


# ── Registry ────────────────────────────────────────────────────────


def test_registry_has_signal_and_basket():
    bots = {b["id"]: b for b in list_bots()}
    for bid in ("breakout", "mean-reversion", "ema-trend", "vwap-reclaim", "mtf-confluence"):
        assert bots[bid]["kind"] == "signal" and "15m" in bots[bid]["timeframes"]
    assert bots["mtf-confluence"]["needs_htf"] and bots["mtf-confluence"]["needs_btc"]
    assert bots["crypto-momentum"]["kind"] == "basket"


# ── Signal bots: fire / no-fire ─────────────────────────────────────


def test_breakout_fires_and_flat():
    fire = get_bot("breakout").evaluate(frame([100] * 39 + [108], vols=[1e6] * 39 + [3e6]), {})
    assert fire["state"] == "long" and fire["entry"] == 108.0 and fire["stop"] < 108 < fire["target"]
    assert fire["r_multiple"] is not None
    flat = get_bot("breakout").evaluate(frame([100] * 40), {})
    assert flat["state"] == "flat"


def test_ema_trend_fires_on_uptrend():
    assert get_bot("ema-trend").evaluate(frame(np.linspace(90, 150, 60)), {})["state"] == "long"
    assert get_bot("ema-trend").evaluate(frame([100] * 60), {})["state"] == "flat"   # no trend


def test_mean_reversion_fires_on_sharp_selloff():
    sharp = [100] * 34 + [99, 97, 94, 90, 85, 80]      # abrupt drop → RSI2≈0, below lower band
    assert get_bot("mean-reversion").evaluate(frame(sharp), {})["state"] == "long"
    assert get_bot("mean-reversion").evaluate(frame([100] * 40), {})["state"] == "flat"


def test_vwap_reclaim_fires():
    # Uptrend (RSI high), a 2-bar pullback under the lagging VWAP, then a bar reclaims it.
    closes = list(np.linspace(90, 112, 45)) + [106, 103, 111]
    assert get_bot("vwap-reclaim").evaluate(frame(closes), {})["state"] == "long"
    # No reclaim in a flat tape.
    assert get_bot("vwap-reclaim").evaluate(frame([100] * 50), {})["state"] == "flat"


def test_mtf_confluence_gated_by_htf_and_btc():
    up = np.linspace(90, 140, 80)                       # rising htf/btc trend
    htf = frame(up, freq="1h")
    btc = frame(up, freq="1h")
    # LTF: pullback to EMA21 then reclaim with rising RSI.
    ltf_closes = list(np.linspace(100, 130, 40)) + [126, 124, 128, 131]
    ltf = frame(ltf_closes)
    long = get_bot("mtf-confluence").evaluate(ltf, {"htf_df": htf, "btc_df": btc, "timeframe": "15m"})
    assert long["state"] == "long" and long["indicators"]["htf_trend"] == "up"
    # Higher-TF trend down but BTC still up → not aligned for a short, no long → blocked.
    htf_down = frame(np.linspace(140, 90, 80), freq="1h")
    blocked = get_bot("mtf-confluence").evaluate(ltf, {"htf_df": htf_down, "btc_df": btc, "timeframe": "15m"})
    assert blocked["state"] == "flat" and blocked["indicators"]["htf_trend"] == "down"


# ── Scanner ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_returns_only_signaling(monkeypatch):
    monkeypatch.setattr(scan_mod, "get_universe_tickers", lambda market, universe: ["UP", "FLAT"])
    frames = {"UP": frame(np.linspace(90, 150, 60)), "FLAT": frame([100] * 60)}

    def fake_klines(sym, timeframe, bars):
        return frames.get(sym, pd.DataFrame())

    monkeypatch.setattr(scan_mod.crypto_data, "get_klines", fake_klines)
    res = await scan_mod.scan("ema-trend", timeframe="15m", universe="Top 50")
    assert res["scanned"] == 2 and [s["ticker"] for s in res["signaling"]] == ["UP"]
    assert res["signaling"][0]["entry"] and res["signaling"][0]["stop"] < res["signaling"][0]["entry"]


@pytest.mark.asyncio
async def test_scan_rejects_basket_bot():
    res = await scan_mod.scan("crypto-momentum", timeframe="15m")
    assert "error" in res


# ── Short side: every bot's mirror setup fires ──────────────────────


def test_reward_risk_both_sides():
    from pipeline_core.bots.base import reward_risk
    assert reward_risk("long", 100, 90, 120) == 2.0
    assert reward_risk("short", 100, 110, 80) == 2.0
    assert reward_risk("long", 100, 110, 120) is None      # stop above entry on a long → invalid
    assert reward_risk("short", 100, 90, 120) is None      # stop below entry on a short → invalid


def test_breakout_shorts_on_breakdown():
    s = get_bot("breakout").evaluate(frame([100] * 39 + [92], vols=[1e6] * 39 + [3e6]), {})
    assert s["side"] == "short" and s["target"] < 92 < s["stop"] and s["r_multiple"] is not None


def test_mean_reversion_shorts_on_sharp_rally():
    sharp = [100] * 34 + [101, 103, 106, 110, 115, 120]    # abrupt spike → RSI2≈100, above upper band
    assert get_bot("mean-reversion").evaluate(frame(sharp), {})["side"] == "short"


def test_ema_trend_shorts_on_downtrend():
    assert get_bot("ema-trend").evaluate(frame(np.linspace(150, 90, 60)), {})["side"] == "short"


def test_vwap_shorts_on_breakdown():
    closes = list(np.linspace(130, 96, 45)) + [100, 103, 95]   # bounce over the lagging VWAP, then lose it
    assert get_bot("vwap-reclaim").evaluate(frame(closes), {})["side"] == "short"


def test_mtf_shorts_when_htf_and_btc_down():
    down = frame(np.linspace(140, 90, 80), freq="1h")
    ltf = frame(list(np.linspace(130, 100, 40)) + [104, 106, 102, 99])   # failed bounce into EMA21
    s = get_bot("mtf-confluence").evaluate(ltf, {"htf_df": down, "btc_df": down, "timeframe": "15m"})
    assert s["side"] == "short" and s["indicators"]["htf_trend"] == "down" and s["indicators"]["btc_regime"] == "risk-off"


@pytest.mark.asyncio
async def test_scan_includes_shorts(monkeypatch):
    monkeypatch.setattr(scan_mod, "get_universe_tickers", lambda market, universe: ["DOWN", "FLAT"])
    frames = {"DOWN": frame(np.linspace(150, 90, 60)), "FLAT": frame([100] * 60)}
    monkeypatch.setattr(scan_mod.crypto_data, "get_klines", lambda sym, tf, bars: frames.get(sym, pd.DataFrame()))
    res = await scan_mod.scan("ema-trend", timeframe="15m", universe="Top 50")
    assert res["short"] == 1 and res["long"] == 0
    row = res["signaling"][0]
    assert row["ticker"] == "DOWN" and row["side"] == "short" and row["stop"] > row["entry"] > row["target"]


# ── Intraday Trade Ideas aggregator ─────────────────────────────────


@pytest.mark.asyncio
async def test_intraday_ideas_aggregates_by_conviction(monkeypatch):
    from pipeline_core.bots import ideas as ideas_mod
    monkeypatch.setattr(ideas_mod.bt, "load_edge", lambda: None)   # pure-conviction path (no edge file)
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda market, universe: ["DOWN", "FLAT"])
    down = frame(np.linspace(150, 90, 120))                # clean downtrend → short ideas
    frames = {"DOWN": down, "FLAT": frame([100] * 120)}
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines",
                        lambda sym, tf, bars: frames.get(sym, down))   # BTC ctx → down too
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50")
    assert res["scanned"] == 2 and len(res["bots"]) == 5
    assert res["counts"]["short"] >= 1 and res["counts"]["long"] == 0
    idea = res["short"][0]
    assert idea["ticker"] == "DOWN" and idea["side"] == "short" and idea["conviction"] >= 1
    assert idea["stop"] > idea["entry"] > idea["target"] and idea["bots"]           # levels + agreeing bots
    assert isinstance(idea.get("spark"), list) and len(idea["spark"]) >= 2          # sparkline data for the why-now card
    # Sorted by conviction then R.
    convs = [i["conviction"] for i in res["short"]]
    assert convs == sorted(convs, reverse=True)
    # Position sizing is attached to every shown idea and the book (no-edge → fixed-fractional path).
    assert res["sizing"]["params"]["enabled"] is True
    assert res["sizing"]["short"]["sized"] == len(res["short"])
    assert res["sizing"]["short"]["total_risk_pct"] <= res["sizing"]["params"]["max_book_risk_pct"] + 1e-9
    for i in res["short"]:
        assert 0.0 <= i["risk_pct"] <= res["sizing"]["params"]["risk_per_trade_pct"] + 1e-9
        assert 0.0 <= i["alloc_pct"] <= res["sizing"]["params"]["max_position_pct"] + 1e-9


@pytest.mark.asyncio
async def test_intraday_ideas_min_conviction_filters(monkeypatch):
    from pipeline_core.bots import ideas as ideas_mod
    monkeypatch.setattr(ideas_mod.bt, "load_edge", lambda: None)   # pure-conviction path (no edge file)
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda market, universe: ["DOWN"])
    down = frame(np.linspace(150, 90, 120))
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda sym, tf, bars: down)
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50", min_conviction=5)
    assert res["counts"]["short"] == 0 and res["counts"]["long"] == 0   # 5-bot agreement is rare


@pytest.fixture
def anyio_backend():
    return "asyncio"
