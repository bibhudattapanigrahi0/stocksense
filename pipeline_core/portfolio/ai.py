"""
AI layer for the portfolio manager — an LLM that *reads* a constructed portfolio and
flags construction/risk trade-offs. Advisory only: it never re-weights the basket.

Same design as `pipeline_core/bots/ai.py`:
  - The deterministic constructor decides the basket; the AI only comments.
  - The LLM reasons ONLY from the numbers provided (weights, sectors, concentration,
    cash, backtest edge, the spec) — it is told not to invent prices or news.
  - Degrades to a deterministic template with no LLM configured, so it works with no
    API key and tests stay network-free.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from pipeline_core.services import llm_service

_SEVERITIES = ("low", "medium", "high")


class RiskNote(BaseModel):
    area: str = Field(description="What the note is about, e.g. concentration | sector | cash | edge")
    note: str = Field(description="One-line, specific observation")
    severity: str = Field(description="low | medium | high")


class PortfolioReview(BaseModel):
    read: str = Field(description="2-3 sentence plain-language read of the construction")
    strengths: list[str] = Field(default_factory=list)
    risks: list[RiskNote] = Field(default_factory=list)


_SYSTEM = (
    "You are a portfolio risk reviewer looking at a QUANTITATIVELY constructed basket. Reason ONLY "
    "from the numbers provided (position weights, sectors, top-3 concentration, cash, weighted "
    "backtest win rate / Sharpe, and the construction recipe) — do NOT invent prices, news, or "
    "fundamentals. Judge portfolio-construction risk: concentration, sector tilt, thin cash buffer, "
    "and weak backtest edge. Each risk severity must be one of: low, medium, high. Be concise."
)


# ── Public API ──────────────────────────────────────────────────────


async def review_portfolio(pf: dict) -> dict:
    """LLM read of a portfolio → {read, strengths[], risks[], llm_mode, source}.
    Falls back to a deterministic template with no LLM configured."""
    parsed = await llm_service.llm_structured(
        prompt=f"Review this portfolio:\n\n{_portfolio_context(pf)}",
        schema=PortfolioReview,
        system_prompt=_SYSTEM,
        temperature=0.3,
        max_tokens=600,
        retries=1,
    )
    if parsed is None:
        out = _template_portfolio(pf)
        out.update(llm_mode=llm_service.llm_mode(), source="rules")
        return out
    risks = []
    for r in parsed.risks:
        sev = r.severity.lower().strip()
        risks.append({"area": r.area.strip(), "note": r.note.strip(),
                      "severity": sev if sev in _SEVERITIES else "medium"})
    return {
        "read": parsed.read.strip(),
        "strengths": [s.strip() for s in parsed.strengths if s.strip()],
        "risks": risks,
        "llm_mode": llm_service.llm_mode(),
        "source": "llm",
    }


# ── Grounding context ───────────────────────────────────────────────


def _portfolio_context(pf: dict) -> str:
    s = pf.get("summary", {}) or {}
    spec = pf.get("spec", {}) or {}
    positions = pf.get("positions", []) or []
    lines = [
        f"Name: {pf.get('name')} · market: {spec.get('market', 'IN')} · basis: {spec.get('basis')} · "
        f"risk: {spec.get('risk')} · weighting: {spec.get('weighting')}"
        + (f" · theme: {spec.get('theme')}" if spec.get("theme") else ""),
        f"{len(positions)} positions · cash {round((pf.get('cash_pct') or 0) * 100, 1)}% · "
        f"top-3 concentration {round((s.get('top3_concentration') or 0) * 100, 1)}% · "
        f"{s.get('sector_count', '?')} sectors · avg conviction {s.get('avg_conviction')}×",
        f"Weighted backtest win rate {s.get('weighted_win_rate')}% · Sharpe {s.get('weighted_sharpe')}",
    ]
    top = sorted(positions, key=lambda p: p.get("weight", 0), reverse=True)[:8]
    if top:
        lines.append("Largest holdings:")
        for p in top:
            lines.append(
                f"- {p.get('ticker')} ({p.get('sector', '—')}): {round((p.get('weight') or 0) * 100, 1)}% "
                f"· conviction {p.get('conviction', 1)}×"
            )
    alloc = s.get("allocation_by_sector") or {}
    if alloc:
        top_sectors = sorted(alloc.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines.append("Sector allocation: " + ", ".join(f"{k} {round(v * 100)}%" for k, v in top_sectors))
    return "\n".join(lines)


# ── Deterministic fallback (no LLM configured) ──────────────────────


def _template_portfolio(pf: dict) -> dict:
    s = pf.get("summary", {}) or {}
    spec = pf.get("spec", {}) or {}
    positions = pf.get("positions", []) or []
    cash = pf.get("cash_pct") or 0
    top3 = s.get("top3_concentration") or 0
    sector_count = s.get("sector_count") or 0
    wr = s.get("weighted_win_rate")
    max_w = max((p.get("weight", 0) for p in positions), default=0)
    top_name = max(positions, key=lambda p: p.get("weight", 0), default={}).get("ticker") if positions else None

    risks: list[dict] = []
    if top3 >= 0.7:
        risks.append({"area": "concentration", "severity": "high",
                      "note": f"Top-3 names are {round(top3 * 100)}% of the book — heavily concentrated."})
    elif top3 >= 0.55:
        risks.append({"area": "concentration", "severity": "medium",
                      "note": f"Top-3 concentration is {round(top3 * 100)}%."})
    if max_w >= 0.25 and top_name:
        risks.append({"area": "position size", "severity": "medium",
                      "note": f"{top_name} alone is {round(max_w * 100)}% — single-name risk."})
    if sector_count and sector_count <= 2 and len(positions) >= 4:
        risks.append({"area": "sector", "severity": "medium",
                      "note": f"Only {sector_count} sector(s) across {len(positions)} names — sector-tilted."})
    alloc = s.get("allocation_by_sector") or {}
    heavy = [(k, v) for k, v in alloc.items() if v >= 0.5]
    for k, v in heavy:
        risks.append({"area": "sector", "severity": "high" if v >= 0.6 else "medium",
                      "note": f"{k} is {round(v * 100)}% of the book — a shock to it hits the whole portfolio."})
    if cash < 0.02 and spec.get("risk") != "aggressive":
        risks.append({"area": "cash", "severity": "low",
                      "note": "Fully invested — no dry powder to add on pullbacks."})
    if isinstance(wr, (int, float)) and wr < 45:
        risks.append({"area": "edge", "severity": "medium",
                      "note": f"Weighted backtest win rate is {wr}% — thin historical edge."})

    strengths: list[str] = []
    if sector_count >= 5:
        strengths.append(f"Diversified across {sector_count} sectors.")
    if 0 < top3 < 0.4:
        strengths.append(f"Balanced sizing — top-3 concentration only {round(top3 * 100)}%.")
    if isinstance(wr, (int, float)) and wr >= 55:
        strengths.append(f"Strong weighted backtest win rate ({wr}%).")
    if cash >= 0.1:
        strengths.append(f"Healthy {round(cash * 100)}% cash buffer for volatility.")
    if not strengths:
        strengths.append("Construction respects the risk preset's position and sector caps.")

    theme_txt = f", tilted to {spec.get('theme')}" if spec.get("theme") else ""
    read = (
        f"A {spec.get('risk', 'balanced')}-risk, {spec.get('basis', 'blend')}-basis basket of "
        f"{len(positions)} names{theme_txt}, {round((1 - cash) * 100)}% invested. "
        + (f"Concentrated (top-3 {round(top3 * 100)}%)." if top3 >= 0.55
           else f"Reasonably spread (top-3 {round(top3 * 100)}%).")
        + (f" Weighted backtest win rate {wr}%." if isinstance(wr, (int, float)) else "")
    )
    return {"read": read, "strengths": strengths, "risks": risks}
