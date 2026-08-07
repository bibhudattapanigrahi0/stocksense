"""CFTC COT metals smart-money source: pure compute/signal/enrich + strategy wiring. Offline."""

import pandas as pd

from pipeline_core.backtest import strategies as strat
from pipeline_core.services import cot


def _rows(n=40, base_long=100, base_short=50, step=5, start="2024-01-02"):
    """Synthetic weekly COT rows; commercial long grows by `step` each week (rising net position)."""
    dates = pd.date_range(start, periods=n, freq="7D")
    return [{
        "report_date_as_yyyy_mm_dd": d.strftime("%Y-%m-%dT00:00:00.000"),
        "comm_positions_long_all": str(base_long + i * step),
        "comm_positions_short_all": str(base_short),
        "noncomm_positions_long_all": "10", "noncomm_positions_short_all": "10",
        "open_interest_all": "1000",
    } for i, d in enumerate(dates)]


def test_compute_cot_frame_index_and_availability_lag():
    df = cot.compute_cot_frame(_rows(n=40), lookback_weeks=30)
    assert df is not None and not df.empty
    # Availability date = report_date + release lag (anti-lookahead), not the report date itself.
    assert df.index[0] == pd.Timestamp("2024-01-02") + pd.Timedelta(days=cot.COT_RELEASE_LAG_DAYS)
    # Monotonically rising commercial net ⇒ the latest reading sits at the top of its range (index 100).
    assert df["cot_comm_net"].iloc[-1] > df["cot_comm_net"].iloc[0]
    assert df["cot_index"].iloc[-1] == 100.0


def test_signal_from_frame_accumulate_and_distribute():
    hi = cot.signal_from_frame(cot.compute_cot_frame(_rows(n=40), 30))
    assert hi["direction"] == "accumulate" and hi["family"] == "cot" and hi["label"] == "COT"
    assert hi["source"] == "cftc-cot" and hi["actors"] == ["Commercials"] and hi["cot_index"] == 100.0
    lo = cot.signal_from_frame(cot.compute_cot_frame(_rows(n=40, base_long=400, step=-5), 30))
    assert lo["direction"] == "distribute"
    assert cot.signal_from_frame(None) is None


def test_signal_neutral_between_thresholds():
    df = pd.DataFrame({"cot_index": [10.0, 50.0, 60.0]},
                      index=pd.date_range("2024-01-01", periods=3))
    sig = cot.signal_from_frame(df)
    assert sig["direction"] == "neutral" and sig["conviction"] is None


def test_series_maps_symbol_and_uses_fetch(monkeypatch):
    monkeypatch.setattr(cot, "_fetch_rows", lambda market_name: _rows(n=30))
    df = cot.series("GC=F")
    assert df is not None and "cot_index" in df.columns
    assert cot.series("NOTAMETAL") is None                 # unmapped symbol → None (no fetch)


def test_enrich_frames_merges_only_mapped_symbols(monkeypatch):
    s = cot.compute_cot_frame(_rows(n=40), 30)
    monkeypatch.setattr(cot, "series", lambda symbol: s if symbol == "GC=F" else None)
    frames = {
        "GC=F": pd.DataFrame({"Close": [1.0, 2.0, 3.0]},
                             index=pd.date_range(s.index[-1], periods=3)),
        "SOMESTOCK": pd.DataFrame({"Close": [1.0]}, index=pd.date_range("2024-01-01", periods=1)),
    }
    cot.enrich_frames(frames)
    assert "cot_index" in frames["GC=F"].columns and frames["GC=F"]["cot_index"].notna().all()
    assert "cot_index" not in frames["SOMESTOCK"].columns   # unmapped → untouched


def test_cot_strategy_inert_without_column():
    st = strat.get_strategy("cot-commercials-follow")
    df = pd.DataFrame({"Close": [1.0] * 300})
    assert not st.entry(df).any() and not st.exit(df).any()   # no cot_index ⇒ inert


def test_cot_strategy_fires_on_setup_plus_price_turn():
    # COT is contrarian: with commercials net-long (setup on), the entry triggers when price turns up
    # (reclaims its 20-DMA). A V-shaped path — decline then recovery — must produce at least one entry.
    st = strat.get_strategy("cot-commercials-follow")
    import numpy as np
    close = np.concatenate([np.linspace(100, 80, 60), np.linspace(80, 120, 60)])
    df = pd.DataFrame({"Close": close, "cot_index": np.full(len(close), 80.0)})
    assert st.entry(df).any()                               # price reclaims the 20-DMA while COT ≥ 75
