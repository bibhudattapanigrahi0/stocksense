"""
AI layer for trading bots — an LLM risk-manager that reviews (never replaces) the
bot's deterministic signal.

Design, consistent with the rest of the platform:
  - The **deterministic math decides** the basket; the AI only *reviews* it.
  - The LLM reasons **only from the numbers we give it** (regime, per-name
    momentum / volatility / return, weights, concentration) — it is told not to
    invent news or prices, so it can't hallucinate a catalyst.
  - It **degrades to a rules-only template** when no LLM is configured
    (`llm_provider=none`), so the bot works with no API key and tests stay
    network-free.
  - An optional `apply_review` overlay can only **de-risk** (down-weight flagged
    names, freeing weight to cash) — never add exposure — and is fully disclosed.

`review_signal(signal)` works for any bot signal shaped like crypto_momentum's
(`regime`, `basket[{ticker, weight, return_30d_pct, return_90d_pct, volatility_pct}]`).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pipeline_core.services import llm_service

logger = logging.getLogger(__name__)


class RiskFlag(BaseModel):
    ticker: str
    note: str = Field(description="One-line risk specific to this holding")
    severity: str = Field(description="low | medium | high")


class BotReview(BaseModel):
    briefing: str = Field(description="2-3 sentence plain-language read of the setup")
    conviction: str = Field(description="proceed | caution | reduce")
    risks: list[RiskFlag] = Field(default_factory=list)


_SYSTEM = (
    "You are a risk manager reviewing a QUANTITATIVE momentum trading bot's proposed "
    "basket. Reason ONLY from the numbers provided — do NOT invent news, prices, or "
    "catalysts. Judge portfolio-construction risk: concentration, high-volatility names, "
    "momentum-crash / mean-reversion risk, and regime fragility. Be concise and specific. "
    "conviction must be one of: proceed, caution, reduce. Never recommend leverage."
)


# ── Public API ──────────────────────────────────────────────────────


async def review_signal(signal: dict) -> dict:
    """LLM review of a bot signal → {briefing, conviction, risks[], llm_mode, source}.
    Falls back to a deterministic template when no LLM is configured."""
    context = _signal_context(signal)
    parsed = await llm_service.llm_structured(
        prompt=f"Review this bot signal and its basket:\n\n{context}",
        schema=BotReview,
        system_prompt=_SYSTEM,
        temperature=0.3,
        max_tokens=600,
        retries=1,
    )
    if parsed is None:
        return {**_template_review(signal), "llm_mode": llm_service.llm_mode(), "source": "rules"}
    conviction = parsed.conviction.lower().strip()
    if conviction not in ("proceed", "caution", "reduce"):
        conviction = "caution"
    return {
        "briefing": parsed.briefing.strip(),
        "conviction": conviction,
        "risks": [r.model_dump() for r in parsed.risks],
        "llm_mode": llm_service.llm_mode(),
        "source": "llm",
    }


async def annotate(signal: dict, apply: bool = False) -> dict:
    """Attach an AI review to a bot signal (and optionally apply its de-risk overlay).
    Keeps the deterministic signal generator pure — the AI is an outer layer."""
    review = await review_signal(signal)
    out = apply_review(signal, review) if apply else dict(signal)
    out["ai"] = review
    return out


def apply_review(signal: dict, review: dict, factor: float = 0.5) -> dict:
    """Bounded, disclosed risk overlay: halve (× `factor`) the weight of any holding the
    review flags **high** severity, moving the freed weight to **cash**. Only ever
    de-risks — never adds exposure or pumps other names. Returns a new signal dict."""
    high = {r["ticker"] for r in (review.get("risks") or []) if str(r.get("severity", "")).lower() == "high"}
    basket = signal.get("basket") or []
    if not high or not basket:
        return signal

    adjusted = []
    for b in basket:
        nb = dict(b)
        if nb["ticker"] in high:
            nb["weight"] = round(nb["weight"] * factor, 4)
        adjusted.append(nb)
    invested = round(sum(b["weight"] for b in adjusted), 4)
    return {
        **signal,
        "basket": adjusted,
        "cash_pct": round(max(0.0, 1.0 - invested), 4),
        "ai_applied": {
            "trimmed": sorted(high),
            "factor": factor,
            "note": f"AI risk overlay reduced high-severity names by {int((1 - factor) * 100)}% "
                    "and moved the freed weight to cash (de-risk only).",
        },
    }


# ── Grounding context + rules-only fallback ─────────────────────────


def _signal_context(signal: dict) -> str:
    regime = signal.get("regime", "unknown")
    lines = [
        f"Regime: {regime} — {signal.get('regime_detail', '')}",
        f"Universe: {signal.get('universe', '?')} · Cash: {int(signal.get('cash_pct', 0) * 100)}%",
    ]
    basket = signal.get("basket") or []
    if not basket:
        lines.append("Basket: EMPTY (holding cash).")
        return "\n".join(lines)
    top3 = round(sum(sorted((b["weight"] for b in basket), reverse=True)[:3]) * 100, 1)
    hv = max(basket, key=lambda b: b.get("volatility_pct", 0))
    lines.append(f"Holdings ({len(basket)}), top-3 concentration {top3}%, highest vol {hv['ticker']} {hv.get('volatility_pct')}%:")
    for b in basket:
        lines.append(
            f"- {b['ticker']}: weight {round(b['weight'] * 100, 1)}% | "
            f"30d {b.get('return_30d_pct')}% | 90d {b.get('return_90d_pct')}% | vol {b.get('volatility_pct')}%"
        )
    return "\n".join(lines)


def _template_review(signal: dict) -> dict:
    """Deterministic review when no LLM is available — still useful, never blank."""
    regime = signal.get("regime")
    basket = signal.get("basket") or []

    if regime == "risk-off" or not basket:
        return {
            "briefing": "Bitcoin is below its trend, so the bot is de-risked and holding cash. "
                        "No new positions until the regime turns risk-on.",
            "conviction": "reduce",
            "risks": [],
        }

    risks = []
    for b in basket:
        vol = b.get("volatility_pct") or 0
        if vol >= 120:
            risks.append({"ticker": b["ticker"], "note": f"Very high volatility ({vol}%) — sized down but still a drawdown risk.", "severity": "high"})
        elif vol >= 90:
            risks.append({"ticker": b["ticker"], "note": f"Elevated volatility ({vol}%).", "severity": "medium"})
    top_w = max((b["weight"] for b in basket), default=0)
    if top_w >= 0.22:
        top = max(basket, key=lambda b: b["weight"])
        risks.append({"ticker": top["ticker"], "note": f"Largest position ({round(top_w * 100)}%) — concentration.", "severity": "medium"})

    # One flag per ticker — keep the highest severity (a high-vol name shouldn't be
    # downgraded to its concentration note).
    _rank = {"high": 3, "medium": 2, "low": 1}
    best: dict[str, dict] = {}
    for r in risks:
        if r["ticker"] not in best or _rank[r["severity"]] > _rank[best[r["ticker"]]["severity"]]:
            best[r["ticker"]] = r
    risks = list(best.values())

    high = any(r["severity"] == "high" for r in risks)
    conviction = "caution" if high else "proceed"
    avg_vol = round(sum(b.get("volatility_pct", 0) for b in basket) / len(basket), 0)
    return {
        "briefing": f"Risk-on: BTC above its trend. Holding {len(basket)} momentum leaders, "
                    f"inverse-vol weighted (avg vol {int(avg_vol)}%). "
                    + ("Some very high-vol names — watch drawdowns." if high else "Volatility looks manageable."),
        "conviction": conviction,
        "risks": risks,
    }
