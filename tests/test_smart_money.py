"""Phase 0 of the smart-money / coattail layer: the market-agnostic registry + normalized
signal, and the seams it feeds. All offline — network sources are monkeypatched."""

import types

import pandas as pd

from pipeline_core.backtest import idea_ranking as ir
from pipeline_core.services import smart_money as sm

# conftest's autouse fixture neutralises sm.get_signal offline; capture the real one (before any
# fixture runs) so the tests that specifically exercise get_signal opt back into the real code path.
_real_get_signal = sm.get_signal


# ── normalize_ownership: wrap the 13F/ownership dict into the normalized signal ──

def test_normalize_ownership_none_and_unavailable():
    assert sm.normalize_ownership(None) is None
    assert sm.normalize_ownership({"available": False}) is None


def test_normalize_ownership_13f_accumulate_is_superset():
    own = {"available": True, "source": "edgar-13f", "qoq_change_pct": 12.0,
           "n_managers": 6, "held_pct_institutions": 4.1, "ticker": "AAPL"}
    sig = sm.normalize_ownership(own)
    assert sig["direction"] == "accumulate"
    assert sig["net_delta_pct"] == 12.0 and sig["n_actors"] == 6
    assert sig["conviction"] == "high"            # |delta|>=10 or breadth>=5
    assert sig["label"] == "13F" and sig["family"] == "ownership"
    # Superset: every original key survives so the existing 🏦 badge renders unchanged.
    assert sig["source"] == "edgar-13f" and sig["held_pct_institutions"] == 4.1


def test_normalize_ownership_13f_distribute_has_no_conviction():
    sig = sm.normalize_ownership({"available": True, "source": "edgar-13f",
                                  "qoq_change_pct": -3.0, "n_managers": 2})
    assert sig["direction"] == "distribute" and sig["conviction"] is None


def test_normalize_ownership_yfinance_snapshot_cannot_judge_direction():
    sig = sm.normalize_ownership({"available": True, "source": "yfinance",
                                  "qoq_change_pct": None, "held_pct_institutions": 55.0})
    assert sig["direction"] is None and sig["conviction"] is None and sig["label"] == "ownership"


# ── get_signal: US baseline + native override + master switch ──

def test_get_signal_us_routes_through_ownership(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    import pipeline_core.services.market_data as md
    monkeypatch.setattr(md, "get_institutional_ownership",
                        lambda symbol, market: {"available": True, "source": "edgar-13f",
                                                "qoq_change_pct": 4.0, "n_managers": 3})
    sig = _real_get_signal("US", "AAPL")
    assert sig["direction"] == "accumulate" and sig["family"] == "ownership"


def test_get_signal_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", False)
    assert _real_get_signal("US", "AAPL") is None


def test_get_signal_native_source_overrides_ownership(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    monkeypatch.setattr(sm, "_ownership_signal", lambda key, symbol: None)   # metals has no ownership
    fake = types.SimpleNamespace(live_signal=lambda symbol: {
        "available": True, "family": "cot", "direction": "accumulate",
        "net_delta_pct": 30.0, "label": "COT", "conviction": "high"})
    monkeypatch.setattr(sm, "_native_module", lambda key: fake if key == "METALS" else None)
    sig = _real_get_signal("METALS", "GC=F")
    assert sig["family"] == "cot" and sig["direction"] == "accumulate" and sig["label"] == "COT"


# ── has_family: drives the confluence denominator per market ──

def test_has_family_equity_always_true(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    assert sm.has_family("US") is True and sm.has_family("IN") is True


def test_has_family_priceonly_follows_native_source(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    monkeypatch.setattr(sm, "_native_module", lambda key: None)          # Phase 0: no source built
    assert sm.has_family("METALS") is False and sm.has_family("CRYPTO") is False
    monkeypatch.setattr(sm, "_native_module", lambda key: object())      # source lands (Phase 1+)
    assert sm.has_family("METALS") is True


def test_has_family_disabled(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", False)
    assert sm.has_family("US") is False


# ── enrich_frames / get_series: US delegates to edgar_13f; no source ⇒ no-op/None ──

def test_enrich_frames_us_delegates_to_edgar(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    import pipeline_core.services.edgar_13f as e
    seen = {}
    monkeypatch.setattr(e, "enrich_frames", lambda frames: seen.setdefault("n", len(frames)))
    sm.enrich_frames("US", {"AAPL": None, "MSFT": None})
    assert seen["n"] == 2


def test_enrich_frames_without_source_is_noop(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    monkeypatch.setattr(sm, "_native_module", lambda key: None)
    sm.enrich_frames("METALS", {"GC=F": None})       # must not raise / must not add columns


def test_get_series_us_delegates_to_edgar(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    import pipeline_core.services.edgar_13f as e
    df = pd.DataFrame({"agg_shares": [1, 2]})
    monkeypatch.setattr(e, "get_institutional_series", lambda ticker: df)
    assert sm.get_series("US", "AAPL") is df


def test_get_series_without_source_returns_none(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    monkeypatch.setattr(sm, "_native_module", lambda key: None)
    assert sm.get_series("METALS", "GC=F") is None


# ── the normalized signal drives the ranking pillar (end-to-end of the abstraction) ──

def test_normalized_signal_drives_smart_money_pillar():
    assert ir._smart_money_supportive({"direction": "accumulate"}) is True
    assert ir._smart_money_supportive({"direction": "distribute"}) is False
    # Back-compat: a raw ownership dict with no normalized direction falls back to the QoQ delta.
    assert ir._smart_money_supportive({"direction": None, "qoq_change_pct": 2.0}) is True
    assert ir._smart_money_supportive({"qoq_change_pct": None}) is None
    assert ir._smart_money_supportive(None) is None


def test_us_gurus_fold_into_tracked_managers(monkeypatch):
    import pipeline_core.services.edgar_13f as e
    monkeypatch.setattr(e.settings, "sm_us_gurus_enabled", True)
    with_gurus = e.tracked_managers()
    assert set(e.GURU_MANAGERS).issubset(with_gurus)
    assert len(with_gurus) == len(e.TRACKED_MANAGERS) + len(e.GURU_MANAGERS)
    monkeypatch.setattr(e.settings, "sm_us_gurus_enabled", False)
    assert e.tracked_managers() == dict(e.TRACKED_MANAGERS)


# ── get_leaders: the coattail 'who's buying now' board ──

def test_get_leaders_disabled(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", False)
    out = sm.get_leaders("CRYPTO")
    assert out["available"] is False and out["leaders"] == []


def test_get_leaders_crypto_dispatches_to_native(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    fake = types.SimpleNamespace(leaders=lambda limit: {"leaders": [{"coin": "BTC"}], "n_traders": 5})
    monkeypatch.setattr(sm, "_native_module", lambda key: fake if key == "CRYPTO" else None)
    out = sm.get_leaders("CRYPTO", 5)
    assert out["available"] is True and out["market"] == "CRYPTO" and out["leaders"][0]["coin"] == "BTC"


def test_get_leaders_per_symbol_market_returns_note(monkeypatch):
    monkeypatch.setattr(sm.settings, "smart_money_enabled", True)
    out = sm.get_leaders("IN")                       # IN bulk deals are per-symbol only
    assert out["available"] is False and "note" in out
