"""Earnings service — date parsing + next/days-to derivation. yfinance + cache mocked.

Imports the real functions at module load so the conftest autouse stub (which neutralises
earnings for the rest of the suite) doesn't mask them here."""

from datetime import date, timedelta

import pytest

from pipeline_core.services import earnings
from pipeline_core.services.earnings import days_to_earnings, earnings_dates, next_earnings_date


def test_next_and_days_from_schedule(monkeypatch):
    today = date.today()
    iso = [(today - timedelta(days=90)).isoformat(),
           (today + timedelta(days=5)).isoformat(),
           (today + timedelta(days=95)).isoformat()]
    monkeypatch.setattr(earnings, "_all_dates", lambda s, m="IN": iso)
    # days_to_earnings calls next_earnings_date via the module; the conftest stub replaced it, so
    # restore the real one (captured at import) for this test.
    monkeypatch.setattr(earnings, "next_earnings_date", next_earnings_date)
    assert next_earnings_date("AAPL", "US") == (today + timedelta(days=5)).isoformat()
    assert days_to_earnings("AAPL", "US") == 5
    assert len(earnings_dates("AAPL", "US")) == 3           # full history for backtests


def test_no_future_earnings_is_none(monkeypatch):
    monkeypatch.setattr(earnings, "_all_dates",
                        lambda s, m="IN": [(date.today() - timedelta(days=10)).isoformat()])
    assert next_earnings_date("X") is None
    assert days_to_earnings("X") is None


def test_all_dates_parses_dedups_sorts(monkeypatch):
    import pandas as pd
    idx = pd.to_datetime(["2020-01-15", "2019-10-20", "2020-01-15"])   # unsorted + duplicate

    class FakeTicker:
        def get_earnings_dates(self, limit=60):
            return pd.DataFrame(index=idx, data={"eps": [1, 2, 3]})

    monkeypatch.setattr(earnings.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(earnings.cache, "set", lambda *a, **k: None)
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", lambda *a, **k: FakeTicker())
    assert earnings._all_dates("AAPL", "US") == ["2019-10-20", "2020-01-15"]


def test_all_dates_graceful_on_failure(monkeypatch):
    monkeypatch.setattr(earnings.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(earnings.cache, "set", lambda *a, **k: None)
    import yfinance

    def boom(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(yfinance, "Ticker", boom)
    assert earnings._all_dates("AAPL", "US") == []          # never raises
