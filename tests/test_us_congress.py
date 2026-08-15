"""US congressional trades → smart-money source (services/us_congress.py).

Covers the keyless House Clerk seam end-to-end WITHOUT network: the XML index parse, the PTR text parse
(the pypdf output shape — jumbled columns, "(partial)" qualifiers, exchanges dropped, scanned→empty), the
disclosure-dated aggregation (anti-lookahead: records dated by the PUBLIC date, never the older
transaction date), the normalized signal, frame enrichment, and the blend into the US smart-money path.
Fixtures are modelled on real 2025 filings. `conftest` neutralises the live feed; these opt back in."""

import numpy as np
import pandas as pd

from pipeline_core.services import smart_money, us_congress as uc


# ── the XML index ────────────────────────────────────────────────────

_INDEX_XML = b"""<?xml version="1.0"?>
<FinancialDisclosures>
  <Member><Last>Evans</Last><First>Dwight</First><FilingType>P</FilingType>
    <StateDst>PA03</StateDst><Year>2025</Year><FilingDate>4/16/2025</FilingDate><DocID>20030179</DocID></Member>
  <Member><Last>Jackson</Last><First>Jonathan</First><FilingType>P</FilingType>
    <StateDst>IL01</StateDst><Year>2025</Year><FilingDate>10/06/2025</FilingDate><DocID>20032232</DocID></Member>
  <Member><Last>Smith</Last><First>Jane</First><FilingType>O</FilingType>
    <Year>2025</Year><FilingDate>1/1/2025</FilingDate><DocID>99999999</DocID></Member>
</FinancialDisclosures>"""


def test_parse_index_keeps_only_ptrs():
    rows = uc.parse_index(_INDEX_XML)
    assert [r["docid"] for r in rows] == ["20030179", "20032232"]     # the 'O' filing is dropped
    assert rows[0]["member"] == "Dwight Evans" and rows[0]["filing_date"] == "2025-04-16"


def test_parse_index_bad_xml_is_empty():
    assert uc.parse_index(b"not xml") == []


# ── the PTR PDF text (pypdf output shape) ────────────────────────────

# Real extraction shape: asset+ticker, then the action + two concatenated dates + amount range on the
# next line. Includes a "(partial)" sale, a buy, and an EXCHANGE (dropped) + a non-tickered row (ignored).
_PTR_TEXT = """ID Owner Asset Transaction Type Date Notification Date Amount
AbbVie Inc. Common Stock (ABBV)
[ST]
S (partial) 04/03/202504/16/2025$1,001 - $15,000
Alphabet Inc. - Class A Common Stock (GOOGL) [ST]
P 04/03/202504/16/2025$15,001 - $50,000
Some Municipal Bond
E 04/03/202504/16/2025$1,001 - $15,000
Widget Options (WDGT) [OP]
E 04/03/202504/16/2025$1,001 - $15,000
Filing ID #20030179"""


def test_parse_ptr_text_extracts_buys_and_sells():
    rows = uc.parse_ptr_text(_PTR_TEXT)
    by_tk = {r["ticker"]: r for r in rows}
    assert set(by_tk) == {"ABBV", "GOOGL"}                 # E (exchange) + non-tickered rows dropped
    assert by_tk["ABBV"]["action"] == "sell"
    assert by_tk["GOOGL"]["action"] == "buy"
    assert by_tk["GOOGL"]["amount"] == (15001 + 50000) / 2  # midpoint of the disclosed range
    assert by_tk["ABBV"]["notif_date"] == "2025-04-16"


def test_parse_ptr_text_empty_on_scanned_or_blank():
    assert uc.parse_ptr_text("") == []                      # a scanned PDF extracts no text
    assert uc.parse_ptr_text("no transactions here") == []


def test_amount_and_date_helpers():
    assert uc._amount_mid("1,001", "15,000") == 8000.5
    assert uc._amount_mid("bad", "x") == 0.0
    assert uc._parse_date("4/16/2025") == "2025-04-16"
    assert uc._parse_date("") is None and uc._parse_date("nope") is None


# ── aggregation → series / signal ────────────────────────────────────

def _recs():
    # Two members net-buy a name (buy 40k+8k, sell 8k → net +40k over the window), disclosure-dated.
    return [
        {"date": "2026-07-01", "member": "Rep A", "action": "buy", "amount": 8000},
        {"date": "2026-07-08", "member": "Rep B", "action": "buy", "amount": 40000},
        {"date": "2026-07-12", "member": "Rep A", "action": "sell", "amount": 8000},
    ]


def test_series_from_records_signs_buys_and_sells():
    s = uc.series_from_records(_recs())
    assert s is not None and "congress_net" in s
    assert s["congress_net"].iloc[-1] == 40000.0            # +8k +40k −8k
    assert uc.series_from_records([]) is None


def test_signal_from_records_direction_and_breadth():
    sig = uc.signal_from_records(_recs())
    assert sig["direction"] == "accumulate" and sig["family"] == "congress"
    assert sig["n_actors"] == 2 and set(sig["actors"]) == {"Rep A", "Rep B"}
    assert "bought" in sig["detail"]
    # Net selling flips the direction.
    sell = [{"date": "2026-07-01", "member": "Rep C", "action": "sell", "amount": 30000}]
    assert uc.signal_from_records(sell)["direction"] == "distribute"


# ── build_panel: bounded, disclosure-dated (anti-lookahead) ──────────

def test_build_panel_dates_by_public_disclosure_not_transaction(monkeypatch):
    monkeypatch.setattr(uc, "_fetch_index", lambda year: [
        {"docid": "1", "member": "Rep A", "year": "2026", "filing_date": "2026-08-01"}])
    # A transaction from months earlier, only disclosed on the notification/filing date.
    monkeypatch.setattr(uc, "_fetch_ptr", lambda docid, year: [
        {"ticker": "NVDA", "action": "buy", "txn_date": "2026-05-01",
         "notif_date": "2026-07-20", "amount": 25000}])
    monkeypatch.setattr(uc.cache, "set", lambda *a, **k: None)      # don't touch the real disk cache

    panel = uc.build_panel(years=[2026], max_filings=10, force=True)
    assert "NVDA" in panel
    # Dated by the DISCLOSURE date (max of notif/filing), NOT the May transaction date → no lookahead.
    assert panel["NVDA"][0]["date"] == "2026-08-01"


def test_build_panel_bounds_to_max_filings(monkeypatch):
    idx = [{"docid": str(i), "member": f"R{i}", "year": "2026",
            "filing_date": f"2026-08-{i:02d}"} for i in range(1, 20)]
    monkeypatch.setattr(uc, "_fetch_index", lambda year: idx)
    seen = []
    def _ptr(docid, year):
        seen.append(docid)
        return [{"ticker": "AAA", "action": "buy", "txn_date": "2026-07-01",
                 "notif_date": "2026-08-01", "amount": 1000}]
    monkeypatch.setattr(uc, "_fetch_ptr", _ptr)
    monkeypatch.setattr(uc.cache, "set", lambda *a, **k: None)

    uc.build_panel(years=[2026], max_filings=5, force=True)
    assert len(seen) == 5                                  # only the 5 most-recently-disclosed processed


# ── enrich_frames + the smart-money blend ────────────────────────────

def _frame():
    idx = pd.date_range("2026-07-01", periods=40, freq="B")
    c = pd.Series(np.linspace(100, 120, 40), index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c}, index=idx)


def test_enrich_frames_merges_congress_net(monkeypatch):
    monkeypatch.setattr(uc, "get_panel", lambda: {"NVDA": _recs()})
    frames = {"NVDA": _frame()}
    uc.enrich_frames(frames)
    assert "congress_net" in frames["NVDA"].columns


def test_enrich_frames_noop_without_panel(monkeypatch):
    monkeypatch.setattr(uc, "get_panel", lambda: None)
    frames = {"NVDA": _frame()}
    uc.enrich_frames(frames)
    assert "congress_net" not in frames["NVDA"].columns


def test_live_signal_reads_the_panel(monkeypatch):
    monkeypatch.setattr(uc, "get_panel", lambda: {"NVDA": _recs()})
    sig = uc.live_signal("NVDA")
    assert sig and sig["family"] == "congress" and sig["direction"] == "accumulate"
    assert uc.live_signal("ZZZZ") is None                  # a name no member traded


def test_blend_us_signals_picks_higher_conviction_and_notes_the_other():
    own = {"available": True, "family": "ownership", "direction": "accumulate",
           "conviction": "low", "label": "13F", "n_actors": 20, "actors": []}
    cong = {"available": True, "family": "congress", "direction": "accumulate",
            "conviction": "high", "label": "Congress", "n_actors": 3, "actors": ["A", "B", "C"],
            "detail": "3 members net bought"}
    b = smart_money._blend_us_signals(own, cong)
    assert b["family"] == "congress"                        # higher-conviction family is the primary face
    assert b.get("families_agree") is True and "secondary" in b
    assert "agrees" in b["detail"]
    # Degenerate inputs: None + X → X (a ticker no member traded is unchanged).
    assert smart_money._blend_us_signals(own, None)["family"] == "ownership"
    assert smart_money._blend_us_signals(None, cong)["family"] == "congress"
    assert smart_money._blend_us_signals(None, None) is None
