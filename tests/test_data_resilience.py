"""Equity data-layer resilience — the price fetch survives a yfinance outage instead of returning an
empty frame that empties the whole board/backtest. Covers: yfinance→Stooq fallback, last-good
stale-serve when every source fails, empty only when nothing has ever succeeded, the Stooq CSV parse,
and per-provider health recording. Network-free (providers stubbed)."""

import pandas as pd
import pytest

from pipeline_core.services import market_data as md, providers


class _MemCache:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, expire=None):
        self.d[k] = v


def _frame(closes, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    return pd.DataFrame({"Close": [float(c) for c in closes]}, index=idx)


@pytest.fixture
def mem(monkeypatch):
    monkeypatch.setattr(md, "cache", _MemCache())
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)   # keep real retry logic, skip the wait
    providers.reset_health()
    return md.cache


# ── Fallback chain ──────────────────────────────────────────────────

def test_yfinance_failure_falls_back_to_stooq(mem, monkeypatch):
    def boom(sym, period, interval, market):
        raise RuntimeError("HTTP 429 rate limited")

    monkeypatch.setattr(md, "_yf_history", boom)
    monkeypatch.setattr(md, "_stooq_history", lambda sym, period, market: _frame([10, 11]))
    df = md.get_price_history("AAPL", "1y", "1d", "US")
    assert float(df["Close"].iloc[-1]) == 11.0
    assert df.attrs.get("source") == "stooq" and df.attrs.get("stale") is False
    # Health recorded both: yfinance failed, stooq succeeded.
    h = {p["provider"]: p for p in providers.provider_health()}
    assert h["yfinance"]["fail"] >= 1 and h["stooq"]["ok"] == 1


def test_success_writes_laststood_copy(mem, monkeypatch):
    monkeypatch.setattr(md, "_yf_history", lambda *a, **k: _frame([5, 6, 7]))
    md.get_price_history("MSFT", "1y", "1d", "US")
    assert mem.get("laststood:price:US:MSFT:1y:1d") is not None      # stale-serve copy kept


def test_stooq_skipped_when_disabled(mem, monkeypatch):
    monkeypatch.setattr(md.settings, "equity_fallback_enabled", False)
    monkeypatch.setattr(md, "_yf_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    called = {"stooq": False}
    monkeypatch.setattr(md, "_stooq_history",
                        lambda *a, **k: called.__setitem__("stooq", True) or _frame([1]))
    out = md.get_price_history("AAPL", "1y", "1d", "US")
    assert out.empty and called["stooq"] is False                   # Stooq not in the chain
    assert "stooq" not in {p["provider"] for p in providers.provider_health()}


# ── Stale-serve ─────────────────────────────────────────────────────

def test_all_sources_fail_serves_stale_cache(mem, monkeypatch):
    mem.set("laststood:price:US:AAPL:1y:1d", _frame([99]))          # a last-good frame from before
    monkeypatch.setattr(md, "_yf_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(md, "_stooq_history", lambda *a, **k: pd.DataFrame())
    df = md.get_price_history("AAPL", "1y", "1d", "US")
    assert float(df["Close"].iloc[-1]) == 99.0                      # served the stale frame…
    assert df.attrs.get("stale") is True and df.attrs.get("source") == "cache"   # …flagged stale


def test_no_data_and_no_cache_returns_empty_frame(mem, monkeypatch):
    monkeypatch.setattr(md, "_yf_history", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(md, "_stooq_history", lambda *a, **k: pd.DataFrame())
    df = md.get_price_history("ZZZ", "1y", "1d", "US")
    assert isinstance(df, pd.DataFrame) and df.empty                # empty frame, never None


def test_cache_hit_short_circuits_before_any_fetch(mem, monkeypatch):
    mem.set("price:US:AAPL:1y:1d", _frame([42]))
    monkeypatch.setattr(md, "_yf_history", lambda *a, **k: pytest.fail("should not fetch on cache hit"))
    df = md.get_price_history("AAPL", "1y", "1d", "US")
    assert float(df["Close"].iloc[-1]) == 42.0


# ── Stooq provider ──────────────────────────────────────────────────

def test_stooq_symbol_mapping():
    assert md._stooq_symbol("AAPL", "US") == "aapl.us"
    assert md._stooq_symbol("RELIANCE", "IN") is None               # no reliable NSE mapping → skip


def test_stooq_history_parses_csv(monkeypatch):
    raw = pd.DataFrame({
        "Date": ["2025-01-03", "2025-01-01", "2025-01-02"],         # out of order on purpose
        "Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3],
        "Close": [10.0, 8.0, 9.0], "Volume": [100, 100, 100],
    })
    monkeypatch.setattr(md.pd, "read_csv", lambda url: raw)
    df = md._stooq_history("AAPL", "1mo", "US")
    assert list(df["Close"]) == [8.0, 9.0, 10.0]                    # sorted ascending by date
    assert str(df.index[0].date()) == "2025-01-01"


def test_stooq_history_empty_on_unknown_symbol(monkeypatch):
    # Stooq returns a 1-column "<b>No data</b>"-style CSV for unknown tickers → treat as empty.
    monkeypatch.setattr(md.pd, "read_csv", lambda url: pd.DataFrame({"Date": ["N/D"]}))
    assert md._stooq_history("NOPE", "1mo", "US").empty


def test_stooq_history_unmapped_market_skips_fetch(monkeypatch):
    monkeypatch.setattr(md.pd, "read_csv", lambda url: pytest.fail("should not fetch for unmapped market"))
    assert md._stooq_history("RELIANCE", "1y", "IN").empty          # IN unmapped → no network
