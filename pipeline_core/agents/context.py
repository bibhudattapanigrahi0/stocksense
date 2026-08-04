"""
StockContext — the shared data bundle for one ticker.

Built once by the Data Ingestion Agent and passed to every analysis agent,
so analysis agents never fetch raw market data themselves (design D4).
This makes them pure functions of context → trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class StockContext:
    """All raw data the analysis agents need for one stock."""

    ticker: str
    market: str = "IN"                                # asset class: IN | US | METALS | CRYPTO
    info: dict = field(default_factory=dict)          # normalized yfinance + NSE profile
    price_history: pd.DataFrame = field(default_factory=pd.DataFrame)  # OHLCV, 1y daily
    financials: dict = field(default_factory=dict)    # income/balance/cashflow statements
    benchmark_history: pd.DataFrame = field(default_factory=pd.DataFrame)  # benchmark for beta
    news_articles: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.info.get("name", self.ticker)

    def has_prices(self, min_rows: int = 50) -> bool:
        return not self.price_history.empty and len(self.price_history) >= min_rows
