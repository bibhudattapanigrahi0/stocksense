"""
Report Generator Agent — consolidates all agent outputs into a markdown
equity research note.

Template-first: the report always renders deterministically from structured
analysis data. When an LLM is configured, only the Executive Summary is
LLM-written and spliced in — a missing/failing LLM never breaks reports.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pipeline_core.services import llm_service

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "*This document is auto-generated decision-support research, not investment "
    "advice. Consult a SEBI-registered investment adviser before acting.*"
)


def _bullets(items: list[str], empty: str = "- None identified") -> str:
    lines = [f"- {item}" for item in items if item]
    return "\n".join(lines) if lines else empty


class ReportGeneratorAgent:
    """Synthesizes analysis dicts into a polished research document."""

    def __init__(self):
        self.name = "Report Generator Agent"

    async def generate_markdown_report(
        self,
        symbol: str,
        name: str,
        fundamental: dict,
        technical: dict,
        sentiment: dict,
        risk: dict,
        composite_score: float,
    ) -> str:
        logger.info(f"[{self.name}] Structuring report for {symbol}")

        exec_summary = await self._executive_summary(
            symbol, name, fundamental, technical, sentiment, risk, composite_score
        )

        piotroski = fundamental.get("piotroski_score")
        piotroski_line = (
            f"| Piotroski F-score | {piotroski}/9 |" if piotroski is not None
            else "| Piotroski F-score | n/a |"
        )
        roce = fundamental.get("roce")
        roce_line = (
            f"| ROCE | {roce * 100:.1f}% |" if roce is not None else "| ROCE | n/a |"
        )
        coverage = fundamental.get("interest_coverage")
        coverage_line = (
            f"| Interest coverage | {coverage:.1f}x |" if coverage is not None
            else "| Interest coverage | n/a |"
        )

        signal = technical.get("signal", "HOLD")
        signal_value = signal.value if hasattr(signal, "value") else signal
        risk_level = risk.get("level", "Medium")
        risk_value = risk_level.value if hasattr(risk_level, "value") else risk_level

        headlines = "\n".join(
            f"- [{a.get('title', 'untitled')}]({a.get('link', '#')}) — {a.get('source', '')}"
            for a in sentiment.get("articles", [])[:5]
        ) or "- No recent headlines captured"

        report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        return f"""# {name} ({symbol}) — Equity Research Note

Generated: {report_date} | Composite score: **{composite_score}/100**

{DISCLAIMER}

---

## 1. Executive Summary & Investment Thesis

{exec_summary}

## 2. Scorecard

| Dimension | Score (0–100) |
|---|---|
| Composite | {composite_score} |
| Fundamental | {fundamental.get('score', 'n/a')} |
| Technical | {technical.get('score', 'n/a')} |
| Sentiment | {sentiment.get('score', 'n/a')} |
| Risk (higher = riskier) | {risk.get('score', 'n/a')} |

## 3. Fundamental Analysis

**Valuation status:** {fundamental.get('valuation_status', 'n/a')}

| Quality metric | Value |
|---|---|
{roce_line}
{piotroski_line}
{coverage_line}

**Strengths**
{_bullets(fundamental.get('strengths', []))}

**Concerns**
{_bullets(fundamental.get('concerns', []))}

**Economic moat**

{fundamental.get('moat_assessment', 'Moat assessment not run (quick depth).')}

## 4. Technical Setup

- Signal: **{signal_value}** (score {technical.get('score', 'n/a')}/100)
- RSI (14): {technical.get('rsi', 'n/a')}
- 50-DMA: {technical.get('sma_50', 'n/a')} | 200-DMA: {technical.get('sma_200', 'n/a')}
- 52-week range: {technical.get('week_52_low', 'n/a')} – {technical.get('week_52_high', 'n/a')} ({technical.get('pct_from_52w_high', 'n/a')}% from high)

**Triggers**
{_bullets(technical.get('triggers', []), empty='- No notable triggers')}

## 5. Market Sentiment

- Consensus: **{sentiment.get('consensus', 'Neutral')}** (score {sentiment.get('score', 'n/a')}/100)
- Method: {sentiment.get('method', 'n/a')}

{sentiment.get('summary', '')}

**Recent headlines**
{headlines}

## 6. Key Risks

**Risk level: {risk_value}** | Annualized volatility: {risk.get('annual_volatility_pct', 'n/a')}% | Beta: {risk.get('beta', 'n/a')} | D/E: {risk.get('debt_to_equity_ratio', 'n/a')}x

{_bullets(risk.get('risk_flags', []), empty='- No elevated risk flags')}

## 7. Final Verdict

{self._verdict(composite_score, signal_value, risk_value)}
"""

    def _verdict(self, composite: float, signal: str, risk_level: str) -> str:
        """Rules-based closing call — deterministic, no LLM required."""
        if composite >= 70 and risk_level != "High":
            stance = "**Bullish** — strong composite fundamentals and technicals with manageable risk."
        elif composite >= 55:
            stance = "**Constructive with caution** — decent quality, but size positions for the flagged risks."
        elif composite >= 40:
            stance = "**Neutral / watchlist** — mixed signals; wait for confirmation on trend or valuation."
        else:
            stance = "**Bearish / avoid** — weak composite scores across multiple dimensions."
        technical_note = (
            f"Technical signal is {signal}; "
            + {"BUY": "momentum supports entry.",
               "HOLD": "no clear momentum edge — stagger entries.",
               "SELL": "momentum is against fresh entry."}.get(signal, "")
        )
        return f"{stance} {technical_note} Revisit after the next quarterly result."

    async def _executive_summary(
        self,
        symbol: str,
        name: str,
        fundamental: dict,
        technical: dict,
        sentiment: dict,
        risk: dict,
        composite_score: float,
    ) -> str:
        """LLM-written summary when available; template fallback otherwise."""
        if llm_service.llm_available():
            prompt = (
                f"Write a 3–4 sentence executive summary for an equity research note on "
                f"the Indian stock {name} ({symbol}). Facts:\n"
                f"- Composite score {composite_score}/100 "
                f"(fundamental {fundamental.get('score')}, technical {technical.get('score')}, "
                f"sentiment {sentiment.get('score')}, risk {risk.get('score')})\n"
                f"- Valuation: {fundamental.get('valuation_status')}; "
                f"technical signal: {technical.get('signal')}; "
                f"sentiment consensus: {sentiment.get('consensus')}; "
                f"risk level: {risk.get('level')}\n"
                f"- Top strengths: {'; '.join(fundamental.get('strengths', [])[:3]) or 'none'}\n"
                f"- Top concerns: {'; '.join(fundamental.get('concerns', [])[:3]) or 'none'}\n"
                "Be direct, professional, and actionable for Indian retail investors. "
                "No markdown headers, just the paragraph."
            )
            try:
                text = await llm_service.llm_complete(
                    prompt=prompt,
                    system_prompt="You are a highly respected SEBI-registered investment analyst.",
                    temperature=0.3,
                    max_tokens=220,
                )
                if text.strip():
                    return text.strip()
            except Exception as e:
                logger.warning(f"[{self.name}] LLM summary failed for {symbol}: {e}")

        # Template fallback
        strengths = fundamental.get("strengths", [])
        concerns = fundamental.get("concerns", [])
        signal = technical.get("signal", "HOLD")
        signal_value = signal.value if hasattr(signal, "value") else signal
        consensus = sentiment.get("consensus", "Neutral")
        consensus_value = consensus.value if hasattr(consensus, "value") else consensus
        return (
            f"{name} ({symbol}) scores {composite_score}/100 on the composite pipeline "
            f"(fundamental {fundamental.get('score', 'n/a')}, technical {technical.get('score', 'n/a')}, "
            f"sentiment {sentiment.get('score', 'n/a')}, risk {risk.get('score', 'n/a')}). "
            f"The stock screens as {fundamental.get('valuation_status', 'fairly valued')} with a "
            f"{signal_value} technical signal and {str(consensus_value).lower()} "
            f"news sentiment. Key supports: {'; '.join(strengths[:2]) or 'none identified'}. "
            f"Key watch-items: {'; '.join(concerns[:2]) or 'none identified'}."
        )
