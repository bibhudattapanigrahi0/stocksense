"""Per-market Fear & Greed gauge — the market-mood companion to the regime tide.

Covers the three source paths of ``market_sentiment.fear_greed(market)``: US ⇒ the real CNN index
(HTTP mocked), CRYPTO ⇒ alternative.me (HTTP mocked), IN/other ⇒ the derived "Market Mood"
(benchmark momentum + inverted vol-index percentile, prices mocked), plus graceful degradation to a
neutral 50 on any outage.

Network-free: `requests.get` and price history are monkeypatched. The real function is bound at
import (`real_fear_greed`) so the conftest fixture that stubs the module attribute to a neutral 50
doesn't hide the live code path under test (mirrors `test_market_regime.real_market_regime`)."""

import numpy as np
import pandas as pd

from pipeline_core.services import market_sentiment as ms
from pipeline_core.services.market_sentiment import fear_greed as real_fear_greed


# ── Synthetic price frames (same shape as the regime tests) ─────────


def _frame(closes):
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    c = np.asarray(closes, float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": np.full(len(c), 1e6)}, index=idx)


UP = _frame(np.linspace(100, 200, 260))         # benchmark well above its 125-DMA → greed
DOWN = _frame(np.linspace(200, 100, 260))        # benchmark well below its 125-DMA → fear
# A flat vol series has no range (percentile of the last point is 1.0), so use trending vol: falling
# ends near its low (calm ⇒ greed), rising ends near its high (stressed ⇒ fear).
VIX_FALLING = _frame(np.linspace(30, 12, 260))
VIX_RISING = _frame(np.linspace(12, 35, 260))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _bypass_cache(monkeypatch):
    monkeypatch.setattr(ms._cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(ms._cache, "set", lambda *a, **k: None)


def _patch_prices(monkeypatch, mapping):
    from pipeline_core.services import market_data

    def fake(symbol, period="1y", interval="1d", market="IN"):
        return mapping.get(symbol, pd.DataFrame())

    monkeypatch.setattr(market_data, "get_price_history", fake)


def _patch_http(monkeypatch, resp_or_exc):
    import requests

    def fake(*a, **k):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(requests, "get", fake)


# ── US — the real CNN Fear & Greed Index ────────────────────────────


def test_us_uses_cnn_index(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_http(monkeypatch, _Resp({"fear_and_greed": {
        "score": 72.0, "rating": "greed", "timestamp": "2026-08-11T00:00:00+00:00"}}))
    r = real_fear_greed("US")
    assert r["value"] == 72 and r["classification"] == "Greed"
    assert r["source"] == "cnn" and r["label"] == "Fear & Greed" and r["stale"] is False


def test_us_cnn_failure_degrades_to_neutral(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_http(monkeypatch, RuntimeError("blocked"))
    r = real_fear_greed("US")
    assert r["value"] == 50 and r["stale"] is True and r["source"] == "cnn"


def test_us_cnn_disabled_falls_back_to_derived(monkeypatch):
    _bypass_cache(monkeypatch)
    monkeypatch.setattr(ms.settings, "fear_greed_cnn_enabled", False)
    _patch_prices(monkeypatch, {"^GSPC": UP, "^VIX": VIX_FALLING})
    r = real_fear_greed("US")
    assert r["source"] == "derived" and r["value"] > 55


# ── CRYPTO — alternative.me (and the no-arg default) ────────────────


def test_crypto_uses_alternative_me(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_http(monkeypatch, _Resp({"data": [
        {"value": "61", "value_classification": "Greed", "timestamp": "1700000000"}]}))
    r = real_fear_greed("CRYPTO")
    assert r["value"] == 61 and r["classification"] == "Greed" and r["source"] == "crypto"


def test_default_market_is_crypto(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_http(monkeypatch, _Resp({"data": [
        {"value": "40", "value_classification": "Fear", "timestamp": "1700000000"}]}))
    r = real_fear_greed()   # no arg keeps every existing crypto caller working
    assert r["source"] == "crypto" and r["value"] == 40


# ── IN / other — the derived "Market Mood" ──────────────────────────


def test_in_derived_greed(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {"^NSEI": UP, "^INDIAVIX": VIX_FALLING})
    r = real_fear_greed("IN")
    assert r["source"] == "derived" and r["label"] == "Market Mood"
    assert r["value"] > 55 and "Greed" in r["classification"]
    assert r["stale"] is False and r["factors"]


def test_in_derived_fear(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {"^NSEI": DOWN, "^INDIAVIX": VIX_RISING})
    r = real_fear_greed("IN")
    assert r["source"] == "derived" and r["value"] < 45 and "Fear" in r["classification"]


def test_in_derived_no_history_degrades(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {})   # every symbol → empty frame ⇒ no sub-scores
    r = real_fear_greed("IN")
    assert r["value"] == 50 and r["stale"] is True and r["source"] == "derived"


def test_classify_bands():
    assert ms._classify(10) == "Extreme Fear"
    assert ms._classify(50) == "Neutral"
    assert ms._classify(90) == "Extreme Greed"
