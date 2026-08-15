"""Shared fixtures: synthetic stock profiles, price frames, and statements."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _hermetic_llm(monkeypatch):
    """Keep the whole suite network-free and deterministic regardless of a local
    .env: default every test to rules-only mode (no provider, no keys). Provider
    tests opt back in via their own monkeypatch, which runs after this fixture."""
    from pipeline_core.services import (
        cot, crypto_smartmoney, earnings, edgar_13f, in_smartmoney, llm_service, market_regime,
        market_sentiment, sector, smart_money, us_congress,
    )

    monkeypatch.setattr(llm_service.settings, "llm_provider", "none")
    monkeypatch.setattr(llm_service.settings, "openai_api_key", "")
    monkeypatch.setattr(llm_service.settings, "anthropic_api_key", "")
    monkeypatch.setattr(llm_service.settings, "gemini_api_key", "")
    llm_service.reset_llm_breaker()   # never carry a tripped circuit breaker between tests

    # Idea-curation sentiment is a live-network source too (Fear & Greed + per-coin news), so
    # neutralise it by default — a neutral 50 reading contributes no bias. Tests that exercise
    # sentiment scoring opt back in with their own monkeypatch (runs after this fixture).
    async def _neutral_news(ticker, name=None):
        return {"score": 50.0, "consensus": "Neutral", "summary": "", "method": "stub"}

    monkeypatch.setattr(market_sentiment, "fear_greed",
                        lambda market="CRYPTO": {"value": 50, "classification": "Neutral", "as_of": None,
                                                 "stale": True, "source": "stub", "label": "Fear & Greed"})
    monkeypatch.setattr(market_sentiment, "coin_news", _neutral_news)

    async def _neutral_news_sent(ticker, name=None, market="IN"):
        return {"score": 50.0, "consensus": "Neutral", "summary": "", "method": "stub"}

    monkeypatch.setattr(market_sentiment, "news_sentiment", _neutral_news_sent)

    # The daily-idea regime gate fetches the benchmark + a vol/macro index (live network too), so
    # neutralise it by default — a neutral tilt=0 reading leaves ordering and sizing unchanged.
    # Tests that exercise the gate opt back in with their own monkeypatch (runs after this fixture).
    monkeypatch.setattr(market_regime, "market_regime",
                        lambda market=None: {"market": (market or "IN"), "state": "neutral",
                                             "score": 50.0, "tilt": 0.0, "benchmark": None,
                                             "volatility": None, "factors": [], "as_of": None,
                                             "stale": True})

    # SEC 13F is a live-network source (EDGAR + OpenFIGI) and also caches a panel to disk;
    # default to "no panel built" so US institutional-ownership falls back to the yfinance
    # snapshot and nothing reads a stale cache. Tests opt back in with their own monkeypatch.
    monkeypatch.setattr(edgar_13f, "get_panel", lambda: None)

    # CFTC COT (metals smart-money) is a live Socrata source; default to "no rows" so the metals
    # smart-money signal/strategy stay inert offline. Tests opt back in with their own monkeypatch.
    monkeypatch.setattr(cot, "_fetch_rows", lambda market_name: [])

    # Crypto smart-money (Hyperliquid) is a live on-chain source; default to an empty book so the
    # crypto signal is inert offline (also guards against a stale disk-cached book). Tests opt back in.
    monkeypatch.setattr(crypto_smartmoney, "build_book",
                        lambda force=False: {"coins": {}, "n_traders": 0, "window": "month", "as_of": None})

    # India smart-money (NSE bulk deals + shareholding pattern) are live, often-blocked sources; default
    # to "no data" so the IN signals/strategy stay inert offline. Tests opt back in with their own monkeypatch.
    monkeypatch.setattr(in_smartmoney, "_fetch_deals", lambda symbol: [])
    monkeypatch.setattr(in_smartmoney, "_fetch_shareholders", lambda symbol: [])

    # US congressional trades (House Clerk PTR disclosures) is a live source with a PDF-parse step; default
    # the panel to "not built" so the congress signal/strategy stay inert offline. Tests opt back in.
    monkeypatch.setattr(us_congress, "get_panel", lambda: None)
    monkeypatch.setattr(us_congress, "_fetch_index", lambda year: [])

    # Earnings dates are a live yfinance source (feeds the idea board's event-risk factor);
    # default to "no known earnings" so the ranking factor is neutral offline.
    monkeypatch.setattr(earnings, "days_to_earnings", lambda sym, market="IN": None)
    monkeypatch.setattr(earnings, "next_earnings_date", lambda sym, market="IN": None)
    monkeypatch.setattr(earnings, "earnings_dates", lambda sym, market="IN": [])

    # Sector relative strength fetches sector-ETF history (live) — default to neutral offline.
    monkeypatch.setattr(sector, "sector_rs", lambda sec, market="IN", lookback_days=63: None)

    # The idea board's live enrichment fetches get_stock_info per ticker (sector + quality factors);
    # neutralise that in-enrich lookup so the board's re-rank stays offline (sector/quality → neutral).
    from pipeline_core.backtest import runner as _runner
    monkeypatch.setattr(_runner, "get_stock_info", lambda sym, market="IN": {})

    # Live enrichment also fetches daily price history per ticker for the correlation de-dup (live
    # network) — default to "no history" so clustering finds nothing and de-dup is a no-op offline.
    # Tests that exercise de-dup supply real frames with their own monkeypatch (runs after this one).
    async def _no_history(sym, period="1y", interval="1d", market="IN", **kw):
        return None

    monkeypatch.setattr(_runner, "aget_price_history", _no_history)

    # snapshot_current_ideas now live-enriches the board before tracking (to capture entry-time
    # factors), which reaches get_quote + the smart-money signal (13F/ownership/COT/flows). Neutralise
    # those so tracking/backtest-run tests stay network-free; tests opt back in with their own monkeypatch.
    monkeypatch.setattr(_runner, "get_quote", lambda symbol, market="IN", max_age=None: None)
    monkeypatch.setattr(smart_money, "get_signal", lambda market, symbol: None)


def make_stock(**overrides) -> dict:
    """A solid mid-cap profile that passes typical quality filters."""
    base = {
        "ticker": "TESTCORP",
        "name": "Test Corp Ltd",
        "sector": "Technology",
        "industry": "Software",
        "exchange": "NSI",
        "market_cap": 5e11,
        "market_cap_cr": 50000.0,
        "price": 1250.0,
        "change_pct": 0.8,
        "volume": 500000,
        "avg_volume": 400000,
        "pe_ratio": 22.0,
        "forward_pe": 19.0,
        "pb_ratio": 4.5,
        "ev_ebitda": 14.0,
        "peg_ratio": 1.6,
        "dividend_yield": 0.012,
        "eps": 55.0,
        "book_value": 280.0,
        "roe": 0.24,
        "debt_to_equity": 25.0,  # yfinance percent → 0.25x
        "free_cash_flow": 2e10,
        "revenue_growth": 0.18,
        "earnings_growth": 0.22,
        "profit_margins": 0.19,
        "operating_margins": 0.25,
        "gross_margins": 0.45,
    }
    base.update(overrides)
    return base


def make_price_history(days: int = 260, start: float = 100.0, trend: float = 0.001) -> pd.DataFrame:
    """Deterministic OHLCV frame with a gentle upward trend."""
    rng = np.arange(days)
    close = start * (1 + trend) ** rng
    index = pd.date_range("2025-01-01", periods=days, freq="B")
    return pd.DataFrame(
        {
            "Open": close * 0.998,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(days, 1_000_000),
        },
        index=index,
    )


def make_financials() -> dict:
    """
    Two years of statements, column-oriented like yfinance .to_dict():
    {Timestamp: {row: value}}. Numbers describe a healthy, improving company.
    """
    col_new, col_old = pd.Timestamp("2025-03-31"), pd.Timestamp("2024-03-31")
    return {
        "income_stmt": {
            col_new: {
                "Total Revenue": 12000.0,
                "Gross Profit": 5400.0,
                "EBIT": 2600.0,
                "Interest Expense": 180.0,
                "Net Income": 1900.0,
                "Diluted EPS": 42.0,
            },
            col_old: {
                "Total Revenue": 10000.0,
                "Gross Profit": 4300.0,
                "EBIT": 2000.0,
                "Interest Expense": 200.0,
                "Net Income": 1500.0,
                "Diluted EPS": 34.0,
            },
        },
        "balance_sheet": {
            col_new: {
                "Total Assets": 15000.0,
                "Current Assets": 6000.0,
                "Current Liabilities": 3000.0,
                "Long Term Debt": 1500.0,
                "Ordinary Shares Number": 45_000_000,
            },
            col_old: {
                "Total Assets": 13000.0,
                "Current Assets": 5000.0,
                "Current Liabilities": 2800.0,
                "Long Term Debt": 1800.0,
                "Ordinary Shares Number": 45_000_000,
            },
        },
        "cashflow": {
            col_new: {"Operating Cash Flow": 2400.0},
            col_old: {"Operating Cash Flow": 1900.0},
        },
        "quarterly_income": {},
    }


@pytest.fixture
def stock_factory():
    return make_stock


@pytest.fixture
def price_history():
    return make_price_history()


@pytest.fixture
def financials():
    return make_financials()
