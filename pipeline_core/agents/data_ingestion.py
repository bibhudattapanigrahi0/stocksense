"""
Data Ingestion Agent — builds StockContext bundles from public sources.

Universe fetch and batched profile ingestion use bounded concurrency;
per-stock failures are logged and skipped, never fatal to the run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import pandas as pd

from pipeline_core.agents.context import StockContext
from pipeline_core.markets import display_ticker, get_market
from pipeline_core.services import market_data, nse_data

logger = logging.getLogger(__name__)

BENCHMARK_SYMBOL = "^NSEI"  # legacy default (IN); resolved per-market via get_market()


class DataIngestionAgent:
    """Sources and normalizes all raw market data."""

    def __init__(self):
        self.name = "Data Ingestion Agent"

    async def fetch_universe(self, index: str = "NIFTY 50", market: str = "IN") -> list[str]:
        """Constituent tickers for the given universe/market (live source → fallback list)."""
        logger.info(f"[{self.name}] Fetching universe for {market}/{index}")
        tickers = await asyncio.to_thread(market_data.get_index_tickers, index, market)
        logger.info(f"[{self.name}] Universe: {len(tickers)} tickers for {market}/{index}")
        return tickers

    async def fetch_stock_data(self, symbol: str, enrich_nse: bool = False, market: str = "IN") -> dict:
        """
        Profile dict for one stock: yfinance base + optional NSE enrichment.
        NSE enrichment is India-only and off by default at universe scale — the
        screener requests it only for survivors via build_context().
        """
        info = await market_data.aget_stock_info(symbol, market)
        if enrich_nse and get_market(market).enrich and "error" not in info:
            info = await nse_data.aenrich_with_nse_data(info)
        return info

    async def fetch_batch(
        self,
        symbols: list[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        market: str = "IN",
    ) -> list[dict]:
        """Bounded-concurrency profile fetch for the whole universe."""
        logger.info(f"[{self.name}] Batch fetching {len(symbols)} profiles ({market})")
        return await market_data.fetch_batch_info(
            symbols, progress_callback=progress_callback, market=market
        )

    async def build_context(
        self,
        symbol: str,
        include_financials: bool = True,
        include_prices: bool = True,
        include_benchmark: bool = True,
        include_news: bool = False,
        enrich_nse: bool = True,
        market: str = "IN",
    ) -> StockContext:
        """
        Full StockContext for deep analysis: profile (NSE-enriched for IN), price
        history, financial statements, benchmark history, optional news.
        Components are fetched concurrently; any single failure degrades
        that component only. Benchmark + fundamentals are market-aware
        (price_only markets skip statements upstream).
        """
        m = get_market(market)
        info_task = self.fetch_stock_data(symbol, enrich_nse=enrich_nse, market=m.key)
        prices_task = (
            market_data.aget_price_history(symbol, period="1y", interval="1d", market=m.key)
            if include_prices else _empty_df()
        )
        fin_task = (
            market_data.aget_financials(symbol, market=m.key)
            if include_financials and m.has_fundamentals else _empty_dict()
        )
        bench_task = (
            market_data.aget_price_history(m.benchmark, period="1y", interval="1d", market=m.key)
            if include_benchmark else _empty_df()
        )
        news_task = self._fetch_news(symbol, m.key) if include_news else _empty_list()

        info, prices, financials, benchmark, news = await asyncio.gather(
            info_task, prices_task, fin_task, bench_task, news_task,
            return_exceptions=True,
        )

        ctx = StockContext(
            ticker=display_ticker(symbol),
            market=m.key,
            info=info if isinstance(info, dict) else {"ticker": symbol, "error": str(info)},
            price_history=prices if isinstance(prices, pd.DataFrame) else pd.DataFrame(),
            financials=financials if isinstance(financials, dict) else {},
            benchmark_history=benchmark if isinstance(benchmark, pd.DataFrame) else pd.DataFrame(),
            news_articles=news if isinstance(news, list) else [],
        )
        return ctx

    async def _fetch_news(self, symbol: str, market: str = "IN") -> list[dict]:
        """News via the sentiment agent's fetcher (kept here to avoid a cycle)."""
        from pipeline_core.agents.sentiment import SentimentAnalysisAgent

        agent = SentimentAnalysisAgent()
        info = await market_data.aget_stock_info(symbol, market)
        return await agent._fetch_news(symbol, info.get("name", symbol))


async def _empty_df():
    return pd.DataFrame()


async def _empty_dict():
    return {}


async def _empty_list():
    return []
