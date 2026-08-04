"""Live-price freshness for realistic gains/losses: get_quote bypasses the 15-min history
cache and re-fetches, micro-caches the scalar, and is the single source _current_price uses.
Network-free (yfinance fetch + cache stubbed)."""

import pandas as pd

from pipeline_core.backtest import runner
from pipeline_core.services import market_data


class _MemCache:
    """In-memory stand-in for the diskcache so tests never touch the real cache dir."""
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, expire=None):
        self.d[k] = v


class _FakeYf:
    def __init__(self, df):
        self._df = df

    def history(self, period, interval):
        return self._df


# ── get_quote: fresh + micro-cached ─────────────────────────────────


def test_get_quote_forces_refresh_and_caches_scalar(monkeypatch):
    monkeypatch.setattr(market_data, "cache", _MemCache())
    calls = []

    def fake_hist(symbol, period="1y", interval="1d", market="IN", force_refresh=False):
        calls.append(force_refresh)
        return pd.DataFrame({"Close": [100.0, 101.5, 103.25]})

    monkeypatch.setattr(market_data, "get_price_history", fake_hist)

    q1 = market_data.get_quote("BTC", "CRYPTO")
    q2 = market_data.get_quote("BTC", "CRYPTO")
    assert q1 == 103.25 and q2 == 103.25          # latest close
    assert calls == [True]                          # fetched once with force_refresh; 2nd from micro-cache


def test_get_quote_none_on_empty(monkeypatch):
    monkeypatch.setattr(market_data, "cache", _MemCache())
    monkeypatch.setattr(market_data, "get_price_history", lambda *a, **k: pd.DataFrame())
    assert market_data.get_quote("XYZ", "IN") is None


# ── force_refresh bypasses the history cache ────────────────────────


def test_force_refresh_skips_stale_cache(monkeypatch):
    mem = _MemCache()
    monkeypatch.setattr(market_data, "cache", mem)
    key = f"price:{market_data.get_market('IN').key}:AAA:1y:1d"
    mem.d[key] = pd.DataFrame({"Close": [1.0]})            # a stale cached frame
    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: _FakeYf(pd.DataFrame({"Close": [2.0]})))

    stale = market_data.get_price_history("AAA", "1y", "1d", "IN")
    assert float(stale["Close"].iloc[-1]) == 1.0          # default path serves the cache

    fresh = market_data.get_price_history("AAA", "1y", "1d", "IN", force_refresh=True)
    assert float(fresh["Close"].iloc[-1]) == 2.0          # force_refresh re-fetches, ignoring the cache


# ── _current_price is just the live quote ───────────────────────────


def test_current_price_delegates_to_quote(monkeypatch):
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 42.0)
    assert runner._current_price("AAA", "IN") == 42.0
