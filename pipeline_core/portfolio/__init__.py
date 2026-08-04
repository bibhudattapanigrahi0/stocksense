"""
Portfolio management layer — construct, persist, and rebalance risk-shaped,
theme-tilted baskets built from the fundamental screening strategies, the
systematic trading strategies, or a blend of both.

Heavy dependencies (orchestrator, backtest runner, market data) are imported
lazily inside the engine functions so importing this package stays cheap and
free of import cycles with `services.run_store`.
"""

from __future__ import annotations

from pipeline_core.portfolio.construct import PortfolioError, construct_portfolio
from pipeline_core.portfolio.rebalance import rebalance_portfolio

__all__ = [
    "PortfolioError",
    "construct_portfolio",
    "rebalance_portfolio",
]
