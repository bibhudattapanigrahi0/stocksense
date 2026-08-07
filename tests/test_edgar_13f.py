"""SEC EDGAR 13F service — parsing, aggregation, and the anti-lookahead series.
Network (SEC + OpenFIGI) is monkeypatched; nothing here touches the wire."""

import pandas as pd
import pytest

from pipeline_core.services import edgar_13f as e

# A namespaced 13F information table with two APPLE rows (must sum) + one BAC row.
SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>1000</value>
    <shrsOrPrnAmt><sshPrnamt>500</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>2000</value>
    <shrsOrPrnAmt><sshPrnamt>300</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>BANK OF AMERICA</nameOfIssuer><cusip>060505104</cusip><value>500</value>
    <shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""


def test_parse_info_table_sums_shares_per_cusip():
    assert e.parse_info_table(SAMPLE_XML) == {"037833100": 800, "060505104": 100}


def test_parse_info_table_bad_xml_is_empty():
    assert e.parse_info_table(b"<not-xml") == {}
    assert e.parse_info_table(b"") == {}


def test_series_qoq_and_filing_date_index():
    recs = [
        {"filed": "2020-05-15", "quarter": "2020-03-31", "shares": 1000, "managers": 2},
        {"filed": "2020-08-14", "quarter": "2020-06-30", "shares": 1200, "managers": 3},
        {"filed": "2020-11-14", "quarter": "2020-09-30", "shares": 900, "managers": 2},
    ]
    s = e.series_from_records(recs)
    # indexed by FILING date (availability) — the built-in anti-lookahead
    assert list(s.index.strftime("%Y-%m-%d")) == ["2020-05-15", "2020-08-14", "2020-11-14"]
    assert s["agg_shares"].tolist() == [1000.0, 1200.0, 900.0]
    assert round(s["qoq_change_pct"].iloc[1], 1) == 20.0     # 1000 → 1200
    assert round(s["qoq_change_pct"].iloc[2], 1) == -25.0    # 1200 → 900
    assert s["n_managers"].tolist() == [2, 3, 2]


def test_series_new_position_is_clipped_not_inf():
    recs = [
        {"filed": "2020-05-15", "quarter": "2020-03-31", "shares": 0, "managers": 0},
        {"filed": "2020-08-14", "quarter": "2020-06-30", "shares": 1000, "managers": 1},
    ]
    s = e.series_from_records(recs)
    assert s["qoq_change_pct"].iloc[1] == 500.0    # 0 → 1000 would be inf; clipped


def test_series_empty_is_none():
    assert e.series_from_records([]) is None


def test_filing_list_keeps_only_13f_hr(monkeypatch):
    sub = {"filings": {"recent": {
        "form": ["13F-HR", "10-K", "13F-HR", "13F-NT"],
        "reportDate": ["2020-03-31", "2019-12-31", "2019-12-31", "2019-09-30"],
        "filingDate": ["2020-05-15", "2020-02-01", "2020-02-14", "2019-11-14"],
        "accessionNumber": ["a1", "x", "a2", "y"],
    }, "files": []}}
    monkeypatch.setattr(e, "_sec_get", lambda url, **k: sub)
    fl = e._filing_list("0001067983")
    assert [f["accession"] for f in fl] == ["a1", "a2"]
    assert fl[0] == {"quarter": "2020-03-31", "filed": "2020-05-15", "accession": "a1"}


def test_map_cusips_batches_and_caches(monkeypatch):
    class _Resp:
        status_code = 200
        def json(self):
            return [{"data": [{"ticker": "AAPL"}]}, {"data": []}]  # 2nd CUSIP unmapped
    monkeypatch.setattr(e.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(e.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(e.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(e.time, "sleep", lambda *_a, **_k: None)
    out = e._map_cusips(["037833100", "999999999"])
    assert out["037833100"] == "AAPL" and out["999999999"] is None


def test_build_panel_aggregates_across_managers(monkeypatch):
    monkeypatch.setattr(e, "TRACKED_MANAGERS", {"0000000001": "A", "0000000002": "B"})
    monkeypatch.setattr(e, "GURU_MANAGERS", {})   # build_panel folds in gurus via tracked_managers()
    monkeypatch.setattr(e, "_filing_list",
                        lambda cik: [{"filed": "2020-05-15", "quarter": "2020-03-31", "accession": f"acc{cik}"}])
    monkeypatch.setattr(e, "_info_table",
                        lambda cik, acc: {"037833100": 100} if cik == "0000000001"
                        else {"037833100": 50, "060505104": 30})
    monkeypatch.setattr(e, "_map_cusips", lambda cs: {"037833100": "AAPL", "060505104": "BAC"})
    monkeypatch.setattr(e.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(e.cache, "set", lambda *a, **k: None)

    panel = e.build_panel(["AAPL", "BAC"], force=True)
    aapl = panel["AAPL"][0]
    assert aapl["shares"] == 150 and aapl["managers"] == 2   # held by both managers, summed
    bac = panel["BAC"][0]
    assert bac["shares"] == 30 and bac["managers"] == 1


def test_build_panel_filters_to_target_tickers(monkeypatch):
    monkeypatch.setattr(e, "TRACKED_MANAGERS", {"0000000001": "A"})
    monkeypatch.setattr(e, "_filing_list",
                        lambda cik: [{"filed": "2020-05-15", "quarter": "2020-03-31", "accession": "z"}])
    monkeypatch.setattr(e, "_info_table", lambda cik, acc: {"037833100": 100, "999999999": 999})
    monkeypatch.setattr(e, "_map_cusips", lambda cs: {"037833100": "AAPL", "999999999": "ZZZZ"})
    monkeypatch.setattr(e.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(e.cache, "set", lambda *a, **k: None)
    panel = e.build_panel(["AAPL"], force=True)   # ZZZZ not a target → dropped
    assert set(panel) == {"AAPL"}
