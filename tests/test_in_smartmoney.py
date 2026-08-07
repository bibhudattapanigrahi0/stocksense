"""India (NSE bulk-deal) smart-money source: pure parse/series/signal + strategy wiring. Offline."""

import numpy as np
import pandas as pd

from pipeline_core.backtest import strategies as strat
from pipeline_core.services import in_smartmoney as inm


def _deal(date, side, qty, client="ACE CAPITAL LLP"):
    return {"date": date, "deal_type": side, "quantity": str(qty), "client": client, "price": "100"}


def test_parse_deals_tidies_and_drops_bad_rows():
    df = inm.parse_deals([
        _deal("07-Aug-2026", "BUY", "1,00,000"),      # Indian comma format ⇒ 100000
        _deal("06-Aug-2026", "SELL", "50000"),
        _deal("05-Aug-2026", "X", "10"),              # bad side ⇒ dropped
        {"date": "not-a-date", "deal_type": "BUY", "quantity": "5", "client": "Z"},  # bad date ⇒ dropped
    ])
    assert list(df["side"]) == ["sell", "buy"]         # sorted by date asc (06 sell, 07 buy)
    assert df["qty"].tolist() == [50000.0, 100000.0]


def test_series_next_day_availability_and_net(monkeypatch):
    monkeypatch.setattr(inm, "_fetch_deals", lambda symbol: [
        _deal("07-Aug-2026", "BUY", "100"), _deal("07-Aug-2026", "SELL", "30"),
        _deal("01-Aug-2026", "SELL", "200")])
    s = inm.series("TATAMOTORS")
    assert s.loc[pd.Timestamp("2026-08-08"), "bulk_net_qty"] == 70     # 100−30, available next day
    assert s.loc[pd.Timestamp("2026-08-02"), "bulk_net_qty"] == -200


def test_live_signal_accumulate_names_the_buyers(monkeypatch):
    monkeypatch.setattr(inm, "_fetch_deals", lambda symbol: [
        _deal("07-Aug-2026", "BUY", "100000", "MUKUL AGRAWAL"),
        _deal("06-Aug-2026", "BUY", "50000", "ASHISH KACHOLIA")])
    sig = inm.live_signal("SOMESTOCK")
    assert sig["direction"] == "accumulate" and sig["family"] == "bulk_deal" and sig["label"] == "Bulk deal"
    assert sig["source"] == "nse-bulk-deals" and sig["n_actors"] == 2 and sig["conviction"] == "high"


def test_live_signal_none_when_no_deals(monkeypatch):
    monkeypatch.setattr(inm, "_fetch_deals", lambda symbol: [])
    assert inm.live_signal("X") is None


def test_enrich_frames_merges_event_column(monkeypatch):
    monkeypatch.setattr(inm, "_fetch_deals", lambda symbol: [_deal("05-Aug-2026", "BUY", "100")])
    monkeypatch.setattr(inm.settings, "sm_in_bulk_deals_enabled", True)
    frames = {"TATAMOTORS": pd.DataFrame({"Close": [1.0, 2, 3, 4]},
                                         index=pd.date_range("2026-08-04", periods=4))}
    inm.enrich_frames(frames)
    df = frames["TATAMOTORS"]
    assert "bulk_net_qty" in df.columns
    assert df.loc[pd.Timestamp("2026-08-06"), "bulk_net_qty"] == 100     # deal 05 → available 06
    assert df.loc[pd.Timestamp("2026-08-04"), "bulk_net_qty"] == 0        # discrete event, 0 elsewhere


def test_strategy_inert_without_column():
    st = strat.get_strategy("bulk-deal-follow")
    df = pd.DataFrame({"Close": [1.0] * 300})
    assert not st.entry(df).any() and not st.exit(df).any()


def test_strategy_fires_on_bulk_buy_with_price_confirm():
    st = strat.get_strategy("bulk-deal-follow")
    n = 300
    close = pd.Series(np.linspace(50, 150, n))          # uptrend ⇒ above 20-DMA
    q = pd.Series([0.0] * n)
    q.iloc[-1] = 100000.0                               # a net-buy bulk deal on the last day
    df = pd.DataFrame({"Close": close.values, "bulk_net_qty": q.values})
    assert bool(st.entry(df).iloc[-1]) is True
