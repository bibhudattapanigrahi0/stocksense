"""
Fundamental Analysis Agent — financial health score, real ROCE, Piotroski
F-score, valuation status, and an LLM moat assessment (when configured).

Consumes a StockContext built by the ingestion agent — never fetches data itself.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pipeline_core.agents.context import StockContext
from pipeline_core.models.schemas import AnalysisDepth
from pipeline_core.services import llm_service

logger = logging.getLogger(__name__)


# ── Statement helpers ──────────────────────────────────────────────
# yfinance .to_dict() is column-oriented: {Timestamp: {row_name: value}}.
# Helpers below tolerate both orientations and minor row-name drift.


def _stmt_rows(stmt: dict) -> dict[str, dict]:
    """Normalize a statement dict to {row_name: {column_key: value}}."""
    if not stmt:
        return {}
    rows: dict[str, dict] = {}
    for col_key, col_val in stmt.items():
        if isinstance(col_val, dict):
            for row_name, value in col_val.items():
                rows.setdefault(row_name, {})[col_key] = value
        else:
            # Already row-oriented
            rows.setdefault(col_key, {}).update(col_val if isinstance(col_val, dict) else {})
    return rows


def _series_recent_first(stmt: dict, *row_names: str) -> list[tuple[Any, float]]:
    """
    Extract (column_key, value) pairs for the first matching row name,
    sorted most-recent-first. Values must be numeric.
    """
    rows = _stmt_rows(stmt)
    for name in row_names:
        if name in rows:
            pairs = []
            for col_key, value in rows[name].items():
                if value is None:
                    continue
                try:
                    fval = float(value)
                except (TypeError, ValueError):
                    continue
                if fval != fval:  # NaN statement cell
                    continue
                pairs.append((col_key, fval))
            pairs.sort(key=lambda p: str(p[0]), reverse=True)
            if pairs:
                return pairs
    return []


def _latest(stmt: dict, *row_names: str) -> Optional[float]:
    series = _series_recent_first(stmt, *row_names)
    return series[0][1] if series else None


def _cagr(newest: float, oldest: float, years: int) -> Optional[float]:
    """Compound annual growth rate; None when signs cross or base is non-positive."""
    if years <= 0 or oldest <= 0 or newest <= 0:
        return None
    return (newest / oldest) ** (1 / years) - 1


# ── Quality metrics (used by the screener's quality stage too) ─────


def compute_roce(financials: dict) -> Optional[float]:
    """
    ROCE = EBIT / Capital Employed, where Capital Employed =
    Total Assets − Current Liabilities. Real computation from statements
    (replaces agy's ROE-copy approximation).
    """
    income = financials.get("income_stmt", {})
    balance = financials.get("balance_sheet", {})

    ebit = _latest(income, "EBIT", "Operating Income")
    total_assets = _latest(balance, "Total Assets")
    current_liab = _latest(balance, "Current Liabilities", "Total Current Liabilities")

    if ebit is None or total_assets is None or current_liab is None:
        return None
    capital_employed = total_assets - current_liab
    if capital_employed <= 0:
        return None
    return ebit / capital_employed


def compute_interest_coverage(financials: dict) -> Optional[float]:
    """EBIT / Interest Expense. None when either side is missing or zero."""
    income = financials.get("income_stmt", {})
    ebit = _latest(income, "EBIT", "Operating Income")
    interest = _latest(income, "Interest Expense", "Interest Expense Non Operating")
    if ebit is None or interest is None or interest == 0:
        return None
    return ebit / abs(interest)


def compute_piotroski(financials: dict) -> tuple[Optional[int], list[str]]:
    """
    Piotroski F-score (0–9) from the two most recent annual periods.
    Returns (score, detail_lines); score is None when statements are missing.
    Criteria unavailable in the data score 0 and say so in the details.
    """
    income = financials.get("income_stmt", {})
    balance = financials.get("balance_sheet", {})
    cashflow = financials.get("cashflow", {})
    if not income or not balance:
        return None, []

    score = 0
    details: list[str] = []

    ni = _series_recent_first(income, "Net Income", "Net Income Common Stockholders")
    ocf = _series_recent_first(cashflow, "Operating Cash Flow", "Total Cash From Operating Activities")
    ta = _series_recent_first(balance, "Total Assets")
    revenue = _series_recent_first(income, "Total Revenue", "Operating Revenue")
    gross_profit = _series_recent_first(income, "Gross Profit")
    ltd = _series_recent_first(balance, "Long Term Debt", "Long Term Debt And Capital Lease Obligation")
    cur_assets = _series_recent_first(balance, "Current Assets", "Total Current Assets")
    cur_liab = _series_recent_first(balance, "Current Liabilities", "Total Current Liabilities")
    shares = _series_recent_first(balance, "Ordinary Shares Number", "Share Issued")

    # ── Profitability (4) ──
    if ni and ni[0][1] > 0:
        score += 1
        details.append("+1 Positive net income")
    if ocf and ocf[0][1] > 0:
        score += 1
        details.append("+1 Positive operating cash flow")
    if len(ni) >= 2 and len(ta) >= 2 and ta[0][1] and ta[1][1]:
        roa_now, roa_prev = ni[0][1] / ta[0][1], ni[1][1] / ta[1][1]
        if roa_now > roa_prev:
            score += 1
            details.append("+1 ROA improving year over year")
    if ni and ocf and ocf[0][1] > ni[0][1]:
        score += 1
        details.append("+1 Operating cash flow exceeds net income (accrual quality)")

    # ── Leverage / liquidity (3) ──
    if len(ltd) >= 2 and len(ta) >= 2 and ta[0][1] and ta[1][1]:
        if ltd[0][1] / ta[0][1] <= ltd[1][1] / ta[1][1]:
            score += 1
            details.append("+1 Long-term leverage not rising")
    elif not ltd and ta:
        score += 1  # no long-term debt on the balance sheet at all
        details.append("+1 No long-term debt reported")
    if len(cur_assets) >= 2 and len(cur_liab) >= 2 and cur_liab[0][1] and cur_liab[1][1]:
        if cur_assets[0][1] / cur_liab[0][1] >= cur_assets[1][1] / cur_liab[1][1]:
            score += 1
            details.append("+1 Current ratio stable or improving")
    if len(shares) >= 2 and shares[0][1] <= shares[1][1] * 1.001:
        score += 1
        details.append("+1 No shareholder dilution")

    # ── Operating efficiency (2) ──
    if len(gross_profit) >= 2 and len(revenue) >= 2 and revenue[0][1] and revenue[1][1]:
        if gross_profit[0][1] / revenue[0][1] >= gross_profit[1][1] / revenue[1][1]:
            score += 1
            details.append("+1 Gross margin stable or improving")
    if len(revenue) >= 2 and len(ta) >= 2 and ta[0][1] and ta[1][1]:
        if revenue[0][1] / ta[0][1] >= revenue[1][1] / ta[1][1]:
            score += 1
            details.append("+1 Asset turnover stable or improving")

    return score, details


def compute_quality_metrics(financials: dict) -> dict:
    """
    All statement-derived metrics in one pass:
    ROCE, interest coverage, Piotroski F, 3y EPS/sales CAGR.
    """
    income = financials.get("income_stmt", {})

    piotroski, piotroski_details = compute_piotroski(financials)

    eps_series = _series_recent_first(income, "Diluted EPS", "Basic EPS")
    eps_growth_3y = (
        _cagr(eps_series[0][1], eps_series[-1][1], len(eps_series) - 1)
        if len(eps_series) >= 2
        else None
    )

    rev_series = _series_recent_first(income, "Total Revenue", "Operating Revenue")
    sales_growth_3y = (
        _cagr(rev_series[0][1], rev_series[-1][1], len(rev_series) - 1)
        if len(rev_series) >= 2
        else None
    )

    return {
        "roce": compute_roce(financials),
        "interest_coverage": compute_interest_coverage(financials),
        "piotroski_score": piotroski,
        "piotroski_details": piotroski_details,
        "eps_growth_3y": eps_growth_3y,
        "sales_growth_3y": sales_growth_3y,
    }


# ── Agent ──────────────────────────────────────────────────────────


class FundamentalAnalysisAgent:
    """Deep-dive fundamental analysis from a shared StockContext."""

    def __init__(self):
        self.name = "Fundamental Analysis Agent"

    async def analyze(self, ctx: StockContext, depth: str = "standard") -> dict:
        """
        Returns: score (0–100), strengths, concerns, valuation_status,
        quality metrics (ROCE/Piotroski/coverage), moat_assessment.
        """
        logger.info(f"[{self.name}] Analyzing fundamentals for {ctx.ticker}")
        info = ctx.info
        if not info or "error" in info:
            return {"score": 0, "error": (info or {}).get("error", "no profile data")}

        quality = compute_quality_metrics(ctx.financials) if ctx.financials else {
            "roce": None, "interest_coverage": None, "piotroski_score": None,
            "piotroski_details": [], "eps_growth_3y": None, "sales_growth_3y": None,
        }

        score, checklists = self._calculate_health_score(info, quality)

        analysis: dict[str, Any] = {
            "score": score,
            "strengths": checklists["strengths"],
            "concerns": checklists["concerns"],
            "valuation_status": self._get_valuation_status(info),
            **quality,
        }

        if depth in (AnalysisDepth.STANDARD.value, AnalysisDepth.DEEP.value, "standard", "deep"):
            analysis["moat_assessment"] = await self._assess_moat(ctx, info)

        return analysis

    def _calculate_health_score(self, info: dict, quality: dict) -> tuple[float, dict]:
        """Rule-based score out of 100 (deterministic; no LLM involved)."""
        score = 50.0
        strengths: list[str] = []
        concerns: list[str] = []

        # 1. ROE
        roe = info.get("roe")
        if roe is not None:
            roe_val = roe * 100
            if roe_val > 20:
                score += 12
                strengths.append(f"High Return on Equity (ROE: {roe_val:.1f}%)")
            elif roe_val > 12:
                score += 5
            else:
                score -= 10
                concerns.append(f"Subpar return on equity (ROE: {roe_val:.1f}%)")
        else:
            concerns.append("ROE data unavailable")

        # 2. Real ROCE from statements (only judged when computable)
        roce = quality.get("roce")
        if roce is not None:
            roce_val = roce * 100
            if roce_val > 20:
                score += 8
                strengths.append(f"Strong capital efficiency (ROCE: {roce_val:.1f}%)")
            elif roce_val < 8:
                score -= 8
                concerns.append(f"Weak capital efficiency (ROCE: {roce_val:.1f}%)")

        # 3. Piotroski F-score
        f_score = quality.get("piotroski_score")
        if f_score is not None:
            if f_score >= 7:
                score += 10
                strengths.append(f"Excellent Piotroski F-score ({f_score}/9)")
            elif f_score <= 3:
                score -= 10
                concerns.append(f"Weak Piotroski F-score ({f_score}/9)")

        # 4. Leverage
        de = info.get("debt_to_equity")
        if de is not None:
            if de < 50:
                score += 10
                strengths.append("Conservative debt load (Debt/Equity < 0.5)")
            elif de > 150:
                score -= 12
                concerns.append(f"Elevated debt-to-equity ratio ({de / 100:.1f}x)")
        else:
            strengths.append("No debt data reported (typically low leverage)")

        # 5. Interest coverage
        coverage = quality.get("interest_coverage")
        if coverage is not None:
            if coverage >= 5:
                score += 5
                strengths.append(f"Comfortable interest coverage ({coverage:.1f}x)")
            elif coverage < 1.5:
                score -= 10
                concerns.append(f"Interest coverage under pressure ({coverage:.1f}x)")

        # 6. Valuation
        pe = info.get("pe_ratio")
        if pe is not None:
            if pe < 15:
                score += 8
                strengths.append(f"Undervalued on trailing P/E basis ({pe:.1f}x)")
            elif pe > 50:
                score -= 8
                concerns.append(f"Rich valuation: High P/E ratio ({pe:.1f}x)")

        # 7. Growth
        rev_growth = info.get("revenue_growth")
        if rev_growth is not None:
            rg_val = rev_growth * 100
            if rg_val > 15:
                score += 8
                strengths.append(f"Strong double-digit revenue growth ({rg_val:.1f}% YoY)")
            elif rg_val < 0:
                score -= 8
                concerns.append(f"Shrinking revenues ({rg_val:.1f}% YoY)")

        sales_3y = quality.get("sales_growth_3y")
        if sales_3y is not None and sales_3y > 0.15:
            score += 4
            strengths.append(f"Sustained 3y sales CAGR of {sales_3y * 100:.1f}%")

        score = max(0.0, min(100.0, score))
        return round(score, 1), {"strengths": strengths, "concerns": concerns}

    @staticmethod
    def _get_valuation_status(info: dict) -> str:
        pe = info.get("pe_ratio")
        if pe is None:
            return "Fairly Valued"
        if pe < 15:
            return "Undervalued"
        if pe > 45:
            return "Overvalued"
        return "Fairly Valued"

    async def _assess_moat(self, ctx: StockContext, info: dict) -> str:
        """LLM moat assessment with an honest fallback when no LLM is configured."""
        if not llm_service.llm_available():
            return (
                "Moat assessment requires an LLM provider (set llm_provider + API key). "
                f"Sector: {info.get('sector', 'n/a')} / Industry: {info.get('industry', 'n/a')}."
            )
        prompt = (
            f"Provide a brief competitive moat analysis for the Indian stock "
            f"{ctx.ticker} ({info.get('name')}).\n"
            f"Sector: {info.get('sector')}\nIndustry: {info.get('industry')}\n"
            f"Describe their competitive advantages, market positioning, and direct "
            f"peer rivalry in India (150-200 words max)."
        )
        try:
            return await llm_service.llm_complete(
                prompt=prompt,
                system_prompt="You are a brilliant Indian equity research analyst.",
                temperature=0.2,
                max_tokens=300,
            )
        except Exception as e:
            logger.error(f"[{self.name}] Moat assessment failed for {ctx.ticker}: {e}")
            return "Moat assessment unavailable (LLM call failed)."
