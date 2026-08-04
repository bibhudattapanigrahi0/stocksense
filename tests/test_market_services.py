"""US / crypto universe services + market-aware market-cap scaling. Network mocked."""

import pytest

from pipeline_core.services import crypto_data, market_data, us_data


class FakeResp:
    def __init__(self, text="", data=None, status_code=200):
        self.text = text
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def no_cache(monkeypatch):
    """Force services to fetch instead of returning a previously cached universe."""
    for mod in (us_data, crypto_data):
        monkeypatch.setattr(mod.cache, "get", lambda *_a, **_k: None)
        monkeypatch.setattr(mod.cache, "set", lambda *_a, **_k: None)


# ── US data ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias,canonical", [
    ("sp500", "S&P 500"), ("S&P500", "S&P 500"), ("SPX", "S&P 500"),
    ("nasdaq100", "NASDAQ-100"), ("ndx", "NASDAQ-100"),
    ("dow", "Dow 30"), ("DJIA", "Dow 30"),
])
def test_us_normalize_index_name(alias, canonical):
    assert us_data.normalize_index_name(alias) == canonical


def test_us_curated_lists_no_network():
    assert len(us_data.get_index_constituents("Dow 30")) == 30
    nasdaq = us_data.get_index_constituents("NASDAQ-100")
    assert "AAPL" in nasdaq and "NVDA" in nasdaq


def test_us_sp500_parses_live_csv(monkeypatch, no_cache):
    csv_text = "Symbol,Name\nAAPL,Apple\nMSFT,Microsoft\nBRK.B,Berkshire\n"
    monkeypatch.setattr(us_data.requests, "get", lambda *a, **k: FakeResp(text=csv_text))
    assert us_data.get_index_constituents("S&P 500") == ["AAPL", "MSFT", "BRK.B"]


def test_us_sp500_falls_back_on_failure(monkeypatch, no_cache):
    def boom(*a, **k):
        raise RuntimeError("blocked")
    monkeypatch.setattr(us_data.requests, "get", boom)
    result = us_data.get_index_constituents("S&P 500")
    assert result == us_data.SP500_FALLBACK
    assert "AAPL" in result


# ── Crypto data ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name,size", [("Top 50", 50), ("Top 100", 100), ("garbage", 100)])
def test_crypto_universe_size(name, size):
    assert crypto_data._universe_size(name) == size


def test_crypto_universe_parses_and_excludes_stablecoins(monkeypatch, no_cache):
    rows = [{"symbol": s} for s in ("btc", "usdt", "eth", "wbtc", "sol", "usdc", "xrp")]
    monkeypatch.setattr(crypto_data.requests, "get", lambda *a, **k: FakeResp(data=rows))
    assert crypto_data.get_universe("Top 3") == ["BTC", "ETH", "SOL"]  # stablecoins/wrapped dropped


def test_crypto_universe_filters_rwa_and_nonlatin(monkeypatch, no_cache):
    # CoinGecko's real top-N is polluted with tokenized treasuries / RWA funds,
    # non-Latin symbols, and tokenized-fiat funds — none have a yfinance series.
    rows = [{"symbol": s} for s in (
        "btc", "usyc", "eth", "jtrsy", "sol", "buidl", "币安人生",
        "susde", "link", "eutbl", "ada",
    )]
    monkeypatch.setattr(crypto_data.requests, "get", lambda *a, **k: FakeResp(data=rows))
    assert crypto_data.get_universe("Top 100") == ["BTC", "ETH", "SOL", "LINK", "ADA"]


def test_crypto_is_tradable_filter():
    assert crypto_data._is_tradable("BTC")
    assert not crypto_data._is_tradable("USYC")        # tokenized treasury
    assert not crypto_data._is_tradable("SUSDE")       # staked stablecoin (USD substring)
    assert not crypto_data._is_tradable("EURC")        # euro stablecoin
    assert not crypto_data._is_tradable("币安人生")      # non-Latin
    assert not crypto_data._is_tradable("WBTC")        # wrapped


# ── Binance OHLCV + CoinGecko profile ───────────────────────────────


def _kline(ts_ms, o, h, l, c, v):
    # Binance kline row (12 fields; only [0..5] are used).
    return [ts_ms, str(o), str(h), str(l), str(c), str(v), ts_ms + 1, "0", 1, "0", "0", "0"]


def test_crypto_get_ohlcv_parses_binance(monkeypatch):
    day = 86_400_000
    rows = [_kline(i * day, 100 + i, 105 + i, 95 + i, 101 + i, 1000 + i) for i in range(3)]
    monkeypatch.setattr(crypto_data.requests, "get", lambda *a, **k: FakeResp(data=rows))
    df = crypto_data.get_ohlcv("BTC", 5)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 3
    assert df["Close"].iloc[-1] == 103.0
    assert str(df.index.tz) == "None"                  # naive index for the engine


def test_crypto_get_ohlcv_empty_when_not_listed(monkeypatch):
    # Binance 400 (symbol not listed) → empty frame so callers fall back to yfinance.
    monkeypatch.setattr(crypto_data.requests, "get", lambda *a, **k: FakeResp(status_code=400, text="Invalid symbol"))
    assert crypto_data.get_ohlcv("HYPE", 5).empty


def test_crypto_get_profile(monkeypatch, no_cache):
    rows = [{"symbol": "eth", "name": "Ethereum", "current_price": 1896.4,
             "market_cap": 2.3e11, "total_volume": 3.7e9, "high_24h": 1920, "low_24h": 1870,
             "price_change_percentage_24h": 1.3}]
    monkeypatch.setattr(crypto_data.requests, "get", lambda *a, **k: FakeResp(data=rows))
    prof = crypto_data.get_profile("ETH")
    assert prof["name"] == "Ethereum" and prof["price"] == 1896.4
    assert crypto_data.get_profile("NOPE") is None


def test_market_data_crypto_price_history_uses_binance(monkeypatch):
    import pandas as pd
    frame = pd.DataFrame({"Open": [1], "High": [1], "Low": [1], "Close": [1.5], "Volume": [9]},
                         index=pd.to_datetime(["2026-07-01"]))
    monkeypatch.setattr(crypto_data, "get_ohlcv", lambda sym, days: frame)
    monkeypatch.setattr(market_data.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(market_data.cache, "set", lambda *_a, **_k: None)
    # yfinance must NOT be called for a Binance-served coin.
    monkeypatch.setattr(market_data.yf, "Ticker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("yfinance used")))
    out = market_data.get_price_history("BTC", period="1y", market="CRYPTO")
    assert out["Close"].iloc[-1] == 1.5


def test_market_data_crypto_info_uses_coingecko(monkeypatch):
    monkeypatch.setattr(crypto_data, "get_profile",
                        lambda s: {"name": "Solana", "price": 75.0, "market_cap": 4.37e10,
                                   "volume": 8.6e8, "day_high": 78, "day_low": 73, "change_pct": 1.3})
    monkeypatch.setattr(market_data.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(market_data.cache, "set", lambda *_a, **_k: None)
    info = market_data.get_stock_info("SOL", market="CRYPTO")
    assert info["name"] == "Solana" and info["sector"] == "Cryptocurrency"
    assert info["market_cap_cr"] == pytest.approx(43.7)   # $43.7B
    assert "error" not in info


def test_crypto_universe_falls_back_on_failure(monkeypatch, no_cache):
    def boom(*a, **k):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(crypto_data.requests, "get", boom)
    result = crypto_data.get_universe("Top 5")
    assert result == crypto_data._FALLBACK[:5]
    assert result[0] == "BTC"


# ── Market-aware market-cap scaling ─────────────────────────────────


class FakeTicker:
    def __init__(self, info):
        self._info = info

    @property
    def info(self):
        return self._info


@pytest.fixture
def fake_yf(monkeypatch):
    info = {"marketCap": 2e12, "longName": "Widget Co", "currentPrice": 100.0}
    monkeypatch.setattr(market_data.yf, "Ticker", lambda *_a, **_k: FakeTicker(info))
    monkeypatch.setattr(market_data.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(market_data.cache, "set", lambda *_a, **_k: None)


def test_market_cap_scaled_per_market(fake_yf):
    us = market_data.get_stock_info("AAPL", market="US")
    assert us["market"] == "US"
    assert us["market_cap_cr"] == pytest.approx(2000.0)      # $2T → 2000 B

    inn = market_data.get_stock_info("RELIANCE", market="IN")
    assert inn["market"] == "IN"
    assert inn["market_cap_cr"] == pytest.approx(200000.0)   # ₹2T → 200000 Cr


def test_metals_market_cap_absent(monkeypatch):
    info = {"marketCap": None, "longName": "Gold", "regularMarketPrice": 2400.0}
    monkeypatch.setattr(market_data.yf, "Ticker", lambda *_a, **_k: FakeTicker(info))
    monkeypatch.setattr(market_data.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(market_data.cache, "set", lambda *_a, **_k: None)
    gold = market_data.get_stock_info("GC=F", market="METALS")
    assert gold["market"] == "METALS"
    assert gold["market_cap_cr"] is None                     # no market cap for metals


# ── Institutional ownership overlay (point-in-time, live confirmation only) ──


def test_institutional_ownership_from_info(monkeypatch):
    info = {"marketCap": 2e12, "longName": "Widget Co", "currentPrice": 100.0,
            "heldPercentInstitutions": 0.657, "heldPercentInsiders": 0.016}
    monkeypatch.setattr(market_data.yf, "Ticker", lambda *_a, **_k: FakeTicker(info))
    monkeypatch.setattr(market_data.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(market_data.cache, "set", lambda *_a, **_k: None)
    own = market_data.get_institutional_ownership("AAPL", market="US")
    assert own["held_pct_institutions"] == pytest.approx(65.7)   # fraction → display %
    assert own["held_pct_insiders"] == pytest.approx(1.6)
    assert own["available"] is True


def test_institutional_ownership_degrades_when_absent(monkeypatch):
    # yfinance frequently omits ownership (unsupported market, thin coverage) — must not raise.
    # (conftest stubs edgar_13f.get_panel→None, so US falls back to the yfinance snapshot here.)
    info = {"marketCap": 1e9, "longName": "No Ownership Co", "currentPrice": 10.0}
    monkeypatch.setattr(market_data.yf, "Ticker", lambda *_a, **_k: FakeTicker(info))
    monkeypatch.setattr(market_data.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(market_data.cache, "set", lambda *_a, **_k: None)
    own = market_data.get_institutional_ownership("XYZ", market="US")
    assert own["held_pct_institutions"] is None
    assert own["held_pct_insiders"] is None
    assert own["available"] is False


def test_us_ownership_prefers_edgar_13f(monkeypatch):
    # US names prefer real SEC 13F data — which carries a quarter-over-quarter change and a
    # tracked-manager count that yfinance can't provide.
    import pandas as pd
    from pipeline_core.services import edgar_13f
    monkeypatch.setattr(market_data, "get_stock_info",
                        lambda s, m="IN": {"shares_outstanding": 1000, "held_pct_insiders": 0.02})
    idx = pd.to_datetime(["2025-11-14", "2026-02-17"])
    ser = pd.DataFrame({"quarter": ["2025-09-30", "2025-12-31"], "agg_shares": [100.0, 150.0],
                        "qoq_change_pct": [10.0, 50.0], "n_managers": [3, 4]}, index=idx)
    monkeypatch.setattr(edgar_13f, "get_institutional_series", lambda t: ser)
    out = market_data.get_institutional_ownership("AAPL", market="US")
    assert out["source"] == "edgar-13f"
    assert out["qoq_change_pct"] == 50.0 and out["n_managers"] == 4
    assert out["held_pct_institutions"] == 15.0          # 150 tracked shares / 1000 outstanding
    assert out["quarter"] == "2025-12-31"


def test_us_ownership_falls_back_to_yfinance_when_no_13f(monkeypatch):
    from pipeline_core.services import edgar_13f
    monkeypatch.setattr(market_data, "get_stock_info",
                        lambda s, m="IN": {"held_pct_institutions": 0.6, "held_pct_insiders": 0.01})
    monkeypatch.setattr(edgar_13f, "get_institutional_series", lambda t: None)   # no panel data
    out = market_data.get_institutional_ownership("AAPL", market="US")
    assert out["source"] == "yfinance" and out["held_pct_institutions"] == 60.0
