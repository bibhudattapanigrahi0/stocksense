"""
Technical Analysis Agent — trend, momentum, and signal crossovers from
the shared price history, producing a BUY / HOLD / SELL recommendation.
"""

from __future__ import annotations

import logging

from pipeline_core.agents import indicators
from pipeline_core.agents.context import StockContext
from pipeline_core.models.schemas import SignalType

logger = logging.getLogger(__name__)


class TechnicalAnalysisAgent:
    """Computes indicators and a 0–100 technical score from a StockContext."""

    def __init__(self):
        self.name = "Technical Analysis Agent"

    async def analyze(self, ctx: StockContext) -> dict:
        logger.info(f"[{self.name}] Running technical indicators for {ctx.ticker}")

        df = ctx.price_history
        if df.empty or len(df) < 50:
            return {
                "score": 50,
                "signal": SignalType.HOLD,
                "summary": "Insufficient price data to compute technicals.",
            }

        close = df["Close"]
        current_price = float(close.iloc[-1])
        result: dict = {}

        # RSI
        rsi_series = indicators.rsi(close, length=14)
        rsi_val = float(rsi_series.iloc[-1])
        if rsi_val != rsi_val:  # NaN guard
            rsi_val = 50.0
        result["rsi"] = round(rsi_val, 2)

        # Moving averages
        sma_50 = indicators.sma(close, 50)
        sma_200 = indicators.sma(close, 200)
        sma_50_val = float(sma_50.iloc[-1]) if sma_50.iloc[-1] == sma_50.iloc[-1] else current_price
        sma_200_val = float(sma_200.iloc[-1]) if sma_200.iloc[-1] == sma_200.iloc[-1] else current_price

        result["sma_50"] = round(sma_50_val, 2)
        result["sma_200"] = round(sma_200_val, 2)
        result["above_50dma"] = current_price > sma_50_val
        result["above_200dma"] = current_price > sma_200_val

        # MACD crossover
        macd_df = indicators.macd(close)
        macd_bullish = bool(macd_df["macd"].iloc[-1] > macd_df["signal"].iloc[-1])
        result["macd_bullish"] = macd_bullish

        # 52-week positioning
        window = df.tail(252) if len(df) >= 252 else df
        high_52w = float(window["High"].max())
        low_52w = float(window["Low"].min())
        result["week_52_high"] = round(high_52w, 2)
        result["week_52_low"] = round(low_52w, 2)
        result["pct_from_52w_high"] = round((current_price - high_52w) / high_52w * 100, 2)

        score, signal, triggers = self._compute_score_and_signal(
            current_price, rsi_val, sma_50_val, sma_200_val, macd_bullish
        )
        result["score"] = score
        result["signal"] = signal
        result["triggers"] = triggers
        result["current_price"] = current_price

        return result

    def _compute_score_and_signal(
        self,
        price: float,
        rsi_val: float,
        sma_50: float,
        sma_200: float,
        macd_bullish: bool,
    ) -> tuple[float, SignalType, list[str]]:
        score = 50.0
        triggers: list[str] = []

        # RSI momentum
        if rsi_val < 30:
            score += 15
            triggers.append(f"Oversold territory (RSI: {rsi_val:.1f})")
        elif rsi_val > 70:
            score -= 15
            triggers.append(f"Overbought territory (RSI: {rsi_val:.1f})")
        elif 40 <= rsi_val <= 60:
            score += 5

        # Trend vs moving averages
        if price > sma_200:
            score += 15
            triggers.append("Trading above key 200-DMA (bullish long term)")
        else:
            score -= 15
            triggers.append("Trading below 200-DMA (bearish long term)")

        if price > sma_50:
            score += 10
            triggers.append("Above 50-DMA (bullish short term)")
        else:
            score -= 5

        # MACD
        if macd_bullish:
            score += 10
            triggers.append("MACD above signal line (momentum bullish)")
        else:
            score -= 5

        score = max(0.0, min(100.0, score))

        if score >= 65:
            signal = SignalType.BUY
        elif score <= 40:
            signal = SignalType.SELL
        else:
            signal = SignalType.HOLD

        return round(score, 1), signal, triggers
