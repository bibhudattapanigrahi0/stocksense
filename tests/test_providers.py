"""Data-provider fallback chain + health, and the Kraken crypto OHLCV source.
Network-free (callables/mocks)."""

import pandas as pd
import pytest

from pipeline_core.services import crypto_data as cd
from pipeline_core.services import providers


class FakeResp:
    def __init__(self, data, status=200):
        self._d = data
        self.status_code = status

    def json(self):
        return self._d


# ── fetch_with_fallback ─────────────────────────────────────────────


def test_primary_used_secondary_untouched():
    providers.reset_health()
    calls = []
    res, name = providers.fetch_with_fallback([
        ("a", lambda: (calls.append("a"), "A")[1]),
        ("b", lambda: (calls.append("b"), "B")[1]),
    ])
    assert res == "A" and name == "a" and calls == ["a"]   # b never called


def test_falls_back_on_empty():
    providers.reset_health()
    res, name = providers.fetch_with_fallback(
        [("a", lambda: None), ("b", lambda: "B")], validate=lambda r: r is not None)
    assert res == "B" and name == "b"


def test_retries_then_next_provider(monkeypatch):
    providers.reset_health()
    monkeypatch.setattr(providers.time, "sleep", lambda *_a: None)   # skip real backoff
    attempts = {"a": 0}

    def a():
        attempts["a"] += 1
        raise RuntimeError("boom")

    res, name = providers.fetch_with_fallback([("a", a), ("b", lambda: "B")], retries=2)
    assert res == "B" and name == "b" and attempts["a"] == 3          # 1 + 2 retries
    h = {x["provider"]: x for x in providers.provider_health()}
    assert h["a"]["fail"] == 3 and h["b"]["ok"] == 1


def test_all_fail_returns_none():
    providers.reset_health()
    res, name = providers.fetch_with_fallback(
        [("a", lambda: None), ("b", lambda: None)], validate=lambda r: r is not None)
    assert res is None and name is None


def test_health_success_rate():
    providers.reset_health()
    providers.fetch_with_fallback([("a", lambda: "X")])
    h = providers.provider_health()[0]
    assert h["provider"] == "a" and h["calls"] == 1 and h["ok"] == 1 and h["success_rate_pct"] == 100.0


# ── Kraken source ───────────────────────────────────────────────────


def _kraken_rows(n=10):
    return [[1_700_000_000 + i * 900, "100", "105", "95", str(100 + i), "101", str(10 + i), 5] for i in range(n)]


def test_kraken_parses(monkeypatch):
    payload = {"error": [], "result": {"XXBTZUSD": _kraken_rows(), "last": 123}}
    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: FakeResp(payload))
    df = cd._kraken_klines("BTC", "15m", 10)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df["Close"].iloc[-1] == 109.0 and str(df.index.tz) == "None"


def test_kraken_unknown_pair_empty(monkeypatch):
    monkeypatch.setattr(cd.requests, "get",
                        lambda *a, **k: FakeResp({"error": ["EQuery:Unknown asset pair"], "result": {}}))
    assert cd._kraken_klines("NOPE", "15m", 10).empty


# ── crypto _fetch_ohlcv routes through the chain ────────────────────


def test_fetch_ohlcv_falls_back_to_kraken(monkeypatch):
    providers.reset_health()
    monkeypatch.setattr(cd, "_binance_klines", lambda s, i, b: pd.DataFrame())   # Binance has nothing
    kdf = pd.DataFrame({"Open": [1], "High": [1], "Low": [1], "Close": [1.5], "Volume": [9]},
                       index=pd.to_datetime(["2026-07-27"]))
    monkeypatch.setattr(cd, "_kraken_klines", lambda s, i, b: kdf)
    out = cd._fetch_ohlcv("XMR", "15m", 20)
    assert out["Close"].iloc[-1] == 1.5                 # served by Kraken
