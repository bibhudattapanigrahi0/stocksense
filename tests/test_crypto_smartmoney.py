"""Crypto (Hyperliquid) smart-money source: pure selection/aggregation + signal/leaders. Offline."""

from pipeline_core.services import crypto_smartmoney as cs


def _lb_row(addr, pnl, av=2_000_000, window="month", name=""):
    return {"ethAddress": addr, "displayName": name, "accountValue": str(av),
            "windowPerformances": [["day", {"pnl": "0", "roi": "0"}],
                                   ["week", {"pnl": "0", "roi": "0"}],
                                   [window, {"pnl": str(pnl), "roi": "0.1"}]]}


def _pos(coin, szi, notional):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(notional)}}


def test_coin_mapping():
    assert cs._coin("BTC-USD") == "BTC" and cs._coin("btc") == "BTC" and cs._coin("ETHUSDT") == "ETH"


def test_select_top_traders_filters_losers_and_small_then_ranks():
    rows = [_lb_row("0xA", 500), _lb_row("0xB", 1500), _lb_row("0xC", -10),
            _lb_row("0xD", 9999, av=100)]           # C is a loser; D is too small
    top = cs.select_top_traders(rows, n=5, window="month", min_account_usd=1_000_000)
    assert [t["addr"] for t in top] == ["0xB", "0xA"]   # winners only, ranked by PnL desc


def test_aggregate_positions_net_gross_and_counts():
    book = cs.aggregate_positions([
        [_pos("BTC", 1.0, 100_000), _pos("ETH", -2.0, 50_000)],
        [_pos("BTC", -0.5, 40_000)],
        [_pos("SOL", 0.0, 999)],                    # zero size ⇒ ignored
    ])
    assert book["BTC"]["net_notional"] == 60_000 and book["BTC"]["gross"] == 140_000
    assert book["BTC"]["n_long"] == 1 and book["BTC"]["n_short"] == 1
    assert book["ETH"]["net_notional"] == -50_000 and book["ETH"]["n_short"] == 1
    assert "SOL" not in book


def test_signal_from_book_directions_and_conviction():
    book = {"coins": {"BTC": {"net_notional": 80_000, "gross": 100_000, "n_long": 8, "n_short": 2}},
            "n_traders": 10, "as_of": "t"}
    sig = cs.signal_from_book(book, "BTC")
    assert sig["direction"] == "accumulate" and sig["family"] == "flow" and sig["label"] == "Whales"
    assert sig["source"] == "hyperliquid" and sig["n_actors"] == 10 and sig["conviction"] == "high"
    short = {"coins": {"SOL": {"net_notional": -50_000, "gross": 60_000, "n_long": 1, "n_short": 5}},
             "n_traders": 6, "as_of": "t"}
    assert cs.signal_from_book(short, "SOL")["direction"] == "distribute"
    assert cs.signal_from_book(book, "DOGE") is None            # coin not tracked


def test_live_signal_reads_the_book(monkeypatch):
    book = {"coins": {"BTC": {"net_notional": 80_000, "gross": 100_000, "n_long": 8, "n_short": 2}},
            "n_traders": 10, "as_of": "t"}
    monkeypatch.setattr(cs, "build_book", lambda force=False: book)
    assert cs.live_signal("BTC-USD")["direction"] == "accumulate"


def test_leaders_ranked_by_abs_net(monkeypatch):
    book = {"coins": {
        "BTC": {"net_notional": 10_000, "gross": 10_000, "n_long": 2, "n_short": 0},
        "HYPE": {"net_notional": -200_000, "gross": 200_000, "n_long": 0, "n_short": 9},
    }, "n_traders": 9, "window": "month", "as_of": "t"}
    monkeypatch.setattr(cs, "build_book", lambda force=False: book)
    out = cs.leaders(10)
    assert [L["coin"] for L in out["leaders"]] == ["HYPE", "BTC"]   # most one-sided first
    assert out["leaders"][0]["bias"] == "short" and out["n_traders"] == 9


def test_series_and_enrich_are_noops():
    assert cs.series("BTC-USD") is None
    frames = {"BTC-USD": object()}
    cs.enrich_frames(frames)                        # live-only ⇒ must be a no-op, must not raise
