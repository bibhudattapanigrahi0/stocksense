"""
Risk Assessment Agent — volatility, beta, leverage, promoter pledge, and
liquidity risks from the shared StockContext. Higher score = higher risk.
"""

from __future__ import annotations

import logging

from pipeline_core.agents import indicators
from pipeline_core.agents.context import StockContext
from pipeline_core.models.schemas import RiskLevel

logger = logging.getLogger(__name__)


class RiskAssessmentAgent:
    """Composite 0–100 risk scoring with human-readable flags."""

    def __init__(self):
        self.name = "Risk Assessment Agent"

    async def analyze(self, ctx: StockContext) -> dict:
        logger.info(f"[{self.name}] Assessing risk for {ctx.ticker}")

        score = 30.0  # base risk (lower is better)
        risk_flags: list[str] = []
        info = ctx.info

        # 1. Price volatility
        annual_vol = None
        df = ctx.price_history
        if not df.empty and len(df) > 30:
            vol = indicators.annualized_volatility_pct(df["Close"])
            if vol == vol:  # NaN guard (empty returns series)
                annual_vol = vol
            if annual_vol is not None and annual_vol > 45:
                score += 25
                risk_flags.append(f"Extreme price volatility (annual vol: {annual_vol:.1f}%)")
            elif annual_vol is not None and annual_vol > 30:
                score += 15
                risk_flags.append(f"Elevated price volatility (annual vol: {annual_vol:.1f}%)")
            elif annual_vol is not None:
                score -= 5
        else:
            risk_flags.append("Limited price history for volatility tracking")

        # 2. Beta vs benchmark (NIFTY 50), when benchmark history is in context
        beta = None
        if not df.empty and not ctx.benchmark_history.empty:
            beta = indicators.beta_vs_benchmark(df["Close"], ctx.benchmark_history["Close"])
            if beta is not None:
                if beta > 1.5:
                    score += 10
                    risk_flags.append(f"High market beta ({beta:.2f}) — amplifies index swings")
                elif beta < 0.8:
                    score -= 5

        # 3. Leverage (yfinance D/E is percent: 150 = 1.5x)
        de_ratio = None
        de_pct = info.get("debt_to_equity")
        if de_pct is not None:
            de_ratio = de_pct / 100
            if de_ratio > 2.0:
                score += 25
                risk_flags.append(f"Extremely high leverage (Debt/Equity: {de_ratio:.1f}x)")
            elif de_ratio > 1.0:
                score += 10
                risk_flags.append(f"Moderate leverage (Debt/Equity: {de_ratio:.1f}x)")
            else:
                score -= 5

        # 4. Promoter pledge (critical India-specific risk)
        pledge_pct = info.get("promoter_pledge")
        if pledge_pct is not None:
            if pledge_pct > 25:
                score += 25
                risk_flags.append(
                    f"High promoter pledge ({pledge_pct:.1f}%) — margin-call threat"
                )
            elif pledge_pct > 5:
                score += 10
                risk_flags.append(f"Promoter pledge present ({pledge_pct:.1f}%)")

        # 5. Liquidity
        avg_vol = info.get("avg_volume")
        if avg_vol is not None and avg_vol < 20000:
            score += 15
            risk_flags.append("Low liquidity: average daily volume under 20k shares")

        score = max(0.0, min(100.0, score))

        if score >= 65:
            level = RiskLevel.HIGH
        elif score >= 40:
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.LOW

        return {
            "score": round(score, 1),
            "level": level,
            "annual_volatility_pct": round(annual_vol, 2) if annual_vol else None,
            "beta": round(beta, 2) if beta is not None else None,
            "debt_to_equity_ratio": round(de_ratio, 2) if de_ratio is not None else None,
            "risk_flags": risk_flags,
        }
