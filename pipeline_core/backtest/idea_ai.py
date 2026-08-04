"""
AI layer for the daily/horizon Trade Ideas — an LLM analyst that *reviews* (never
replaces) the quantitative idea, plus a post-mortem over the tracker's realized record.

Design, consistent with `pipeline_core/bots/ai.py`:
  - The **deterministic screen decides** the idea; the AI only annotates it.
  - The LLM reasons **only from the numbers we pass** (levels, conviction, win rate,
    R:R, age) plus a news-sentiment summary — it is told not to invent prices/news.
  - It **degrades to a deterministic template** when no LLM is configured
    (`llm_provider=none`), so everything works with no API key and tests stay
    network-free.

`analyze_idea(idea, market)` works on any idea-shaped dict — a `horizon_ideas`
candidate or a tracked idea (both carry ticker/levels/horizon/conviction/strategies).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pipeline_core.agents import sentiment as sentiment_agent
from pipeline_core.services import llm_service

logger = logging.getLogger(__name__)

_VERDICTS = ("buy", "watch", "avoid")


# ── Schemas ─────────────────────────────────────────────────────────


class IdeaBriefing(BaseModel):
    thesis: str = Field(description="2-3 sentence plain-language read of the setup")
    verdict: str = Field(description="buy | watch | avoid")
    key_risk: str = Field(description="The single biggest risk to this idea, one line")
    invalidation: str = Field(description="The price/event that would kill the thesis")


class TrackPostmortem(BaseModel):
    summary: str = Field(description="3-4 sentence read of the realized track record")
    lessons: list[str] = Field(default_factory=list, description="Concrete, actionable takeaways")


_IDEA_SYSTEM = (
    "You are a trading analyst reviewing a QUANTITATIVE trade idea produced by a strategy "
    "backtester. Reason ONLY from the numbers provided (entry/target/stop levels, reward:risk, "
    "how many strategies agree, their backtest win rate, the idea's age) and the supplied news-"
    "sentiment summary — do NOT invent prices, news, or catalysts. Judge whether the setup is "
    "worth acting on now. verdict must be exactly one of: buy, watch, avoid. Be concise and specific."
)

_TRACK_SYSTEM = (
    "You are a trading coach reviewing the realized track record of a systematic trade-idea book. "
    "Reason ONLY from the tallies provided (closed count, win rate, targets vs stops vs time-exits, "
    "average return, per-horizon breakdown) — do NOT invent trades. Explain what is working and give "
    "concrete, non-generic lessons. Be honest about weak spots."
)


# ── Public API ──────────────────────────────────────────────────────


async def analyze_idea(idea: dict, market: str = "IN", with_sentiment: bool = True) -> dict:
    """LLM analyst read of one trade idea → {thesis, verdict, key_risk, invalidation,
    sentiment, llm_mode, source}. Falls back to a deterministic template with no LLM."""
    sent = None
    if with_sentiment:
        try:
            sent = await sentiment_agent.analyze_ticker_sentiment(
                idea.get("ticker", ""), idea.get("name"), market
            )
        except Exception as e:  # news is best-effort — never block the analyst on it
            logger.warning("Idea sentiment fetch failed for %s: %s", idea.get("ticker"), e)

    context = _idea_context(idea, market, sent)
    parsed = await llm_service.llm_structured(
        prompt=f"Review this trade idea:\n\n{context}",
        schema=IdeaBriefing,
        system_prompt=_IDEA_SYSTEM,
        temperature=0.3,
        max_tokens=500,
        retries=1,
    )
    if parsed is None:
        out = _template_idea(idea, sent)
        out.update(sentiment=_sentiment_brief(sent), llm_mode=llm_service.llm_mode(), source="rules")
        return out

    verdict = parsed.verdict.lower().strip()
    if verdict not in _VERDICTS:
        verdict = "watch"
    return {
        "thesis": parsed.thesis.strip(),
        "verdict": verdict,
        "key_risk": parsed.key_risk.strip(),
        "invalidation": parsed.invalidation.strip(),
        "sentiment": _sentiment_brief(sent),
        "llm_mode": llm_service.llm_mode(),
        "source": "llm",
    }


async def analyze_track_record(status: dict) -> dict:
    """LLM post-mortem over `tracked_ideas_status` output → {summary, lessons[], llm_mode, source}.
    Falls back to a deterministic template with no LLM."""
    context = _track_context(status)
    parsed = await llm_service.llm_structured(
        prompt=f"Review this trade-idea track record:\n\n{context}",
        schema=TrackPostmortem,
        system_prompt=_TRACK_SYSTEM,
        temperature=0.3,
        max_tokens=500,
        retries=1,
    )
    if parsed is None:
        out = _template_postmortem(status)
        out.update(llm_mode=llm_service.llm_mode(), source="rules")
        return out
    return {
        "summary": parsed.summary.strip(),
        "lessons": [ln.strip() for ln in parsed.lessons if ln.strip()],
        "llm_mode": llm_service.llm_mode(),
        "source": "llm",
    }


# ── Grounding context ───────────────────────────────────────────────


def _strategy_names(idea: dict) -> list[str]:
    """Flagging-strategy names — handles both shapes (candidate dicts / tracked strings)."""
    out = []
    for s in idea.get("strategies") or []:
        out.append(s.get("name", "") if isinstance(s, dict) else str(s))
    return [n for n in out if n]


def _pct(frac) -> str:
    return f"{round(frac * 100, 1)}%" if isinstance(frac, (int, float)) else "n/a"


def _idea_context(idea: dict, market: str, sent: dict | None) -> str:
    names = _strategy_names(idea)
    lines = [
        f"Ticker: {idea.get('ticker')} ({(market or 'IN').upper()}) · horizon: {idea.get('horizon', '?')}",
        f"Now ~{idea.get('price')} · buy zone {idea.get('buy_low')}–{idea.get('buy_high')} · "
        f"target {idea.get('target_price')} ({_pct(idea.get('target_pct'))}) · "
        f"stop {idea.get('stop_price')} ({_pct(idea.get('stop_pct'))})",
        f"Reward:risk {idea.get('risk_reward')}:1 · conviction {idea.get('conviction', len(names))} "
        f"strateg{'y' if idea.get('conviction') == 1 else 'ies'} agree · "
        f"avg backtest win rate {idea.get('avg_win_rate')}%",
    ]
    if names:
        lines.append(f"Flagging strategies: {', '.join(names[:8])}")
    if idea.get("age_days") is not None:
        lines.append(f"Signal age: {idea['age_days']} day(s) old")
    if sent:
        lines.append(
            f"News sentiment: {sent.get('consensus', 'Neutral')} {sent.get('score')}/100 — "
            f"{sent.get('summary', '')}"
        )
    return "\n".join(lines)


def _track_context(status: dict) -> str:
    cb = status.get("closed_scoreboard", {}) or {}
    closed = status.get("closed", []) or []
    by_h: dict[str, int] = {}
    for c in closed:
        by_h[c.get("horizon", "?")] = by_h.get(c.get("horizon", "?"), 0) + 1
    horizon_line = ", ".join(f"{h}: {n}" for h, n in by_h.items()) or "none"
    recent = "; ".join(
        f"{c.get('ticker')} {c.get('horizon')} {c.get('exit_reason')} {c.get('realized_return_pct')}%"
        for c in closed[:5]
    )
    return "\n".join([
        f"Closed trades: {cb.get('closed', 0)} · win rate {cb.get('win_rate')}% · "
        f"avg {cb.get('avg_return_pct')}% · total realized {cb.get('total_return_pct')}%",
        f"Exits: {cb.get('targets_hit', 0)} targets / {cb.get('stopped', 0)} stops / "
        f"{cb.get('expired', 0)} time-expired",
        f"Still open: {len(status.get('open', []) or [])}",
        f"Closed by horizon: {horizon_line}",
        f"Most recent closes: {recent or 'none'}",
    ])


def _sentiment_brief(sent: dict | None) -> dict | None:
    """Trim the sentiment payload to what the UI shows (drop the full article list)."""
    if not sent:
        return None
    return {
        "score": sent.get("score"),
        "consensus": sent.get("consensus"),
        "summary": sent.get("summary"),
        "method": sent.get("method"),
        "article_count": len(sent.get("articles", []) or []),
    }


# ── Deterministic fallbacks (no LLM configured) ─────────────────────


def _template_idea(idea: dict, sent: dict | None) -> dict:
    """Useful, never-blank read derived from the idea's own numbers + sentiment."""
    conv = idea.get("conviction") or len(_strategy_names(idea)) or 1
    rr = idea.get("risk_reward") or 0
    wr = idea.get("avg_win_rate") or 0
    consensus = (sent or {}).get("consensus", "Neutral")

    # Small, transparent scorecard → verdict.
    score = 0
    score += 2 if conv >= 3 else 1 if conv == 2 else 0
    score += 1 if rr >= 2 else 0
    score += 1 if wr >= 55 else 0
    score += 1 if consensus == "Bullish" else -2 if consensus == "Bearish" else 0

    # Is price still inside the accumulation zone, or has it run away from it? Chasing an
    # extended entry meaningfully worsens the edge, so it can pull a strong idea down to "watch".
    price, hi = idea.get("price"), idea.get("buy_high")
    extended = isinstance(price, (int, float)) and isinstance(hi, (int, float)) and price > hi * 1.02
    if extended:
        score -= 2

    verdict = "buy" if score >= 3 else "avoid" if score <= 0 else "watch"

    conv_txt = f"{conv} strateg{'y' if conv == 1 else 'ies'} agree"
    rr_txt = f"reward:risk {rr}:1" if rr else "no fixed target (trend-follow)"
    thesis = (
        f"{conv_txt} on this {idea.get('horizon', '')} setup ({rr_txt}, "
        f"{wr}% avg backtest win rate). News reads {consensus.lower()}. "
        + ("Price has run above the buy zone — the edge is worse chasing it here."
           if extended else "Price is still within the suggested buy zone.")
    )
    if consensus == "Bearish":
        key_risk = "News sentiment is bearish — headline risk works against the technical setup."
    elif extended:
        key_risk = "Chasing an extended entry — a pullback to the buy zone would improve the entry."
    elif conv <= 1:
        key_risk = "Thin conviction — only one strategy flags this, so the signal is less corroborated."
    else:
        key_risk = "A market-wide risk-off move can stop out even a well-corroborated single name."
    stop = idea.get("stop_price")
    invalidation = (f"A daily close below the {stop} stop invalidates the idea."
                    if stop is not None else "A break of the strategy's stop invalidates the idea.")
    return {"thesis": thesis, "verdict": verdict, "key_risk": key_risk, "invalidation": invalidation}


def _template_postmortem(status: dict) -> dict:
    cb = status.get("closed_scoreboard", {}) or {}
    closed = status.get("closed", []) or []
    n = cb.get("closed", 0)
    if not n:
        return {"summary": "No trades have closed yet — the track record starts once ideas hit a "
                           "target, stop, or time-stop.", "lessons": []}

    wr = cb.get("win_rate")
    targets, stops, expired = cb.get("targets_hit", 0), cb.get("stopped", 0), cb.get("expired", 0)
    summary = (
        f"{n} closed trade(s): win rate {wr}%, average realized {cb.get('avg_return_pct')}% "
        f"(total {cb.get('total_return_pct')}%). Exits were {targets} target(s), {stops} stop(s), "
        f"and {expired} time-expired."
    )

    lessons: list[str] = []
    if stops > targets:
        lessons.append("More stops than targets — entries may be early; consider waiting for confirmation "
                       "or tightening the buy zone.")
    if expired >= max(1, n // 3):
        lessons.append("A lot of trades time-expire without resolving — the horizon may be too short for "
                       "these setups, or targets too far.")
    if isinstance(wr, (int, float)) and wr >= 55:
        lessons.append("Win rate is above 55% — the edge is positive; keep position sizing consistent.")
    elif isinstance(wr, (int, float)) and wr < 45:
        lessons.append("Win rate is below 45% — review which strategies/horizons are dragging and prune them.")

    # Best-performing horizon by realized return.
    by_h: dict[str, list] = {}
    for c in closed:
        r = c.get("realized_return_pct")
        if r is not None:
            by_h.setdefault(c.get("horizon", "?"), []).append(r)
    if by_h:
        best = max(by_h, key=lambda h: sum(by_h[h]) / len(by_h[h]))
        lessons.append(f"'{best}' horizon has the best average realized return so far "
                       f"({round(sum(by_h[best]) / len(by_h[best]), 1)}%).")
    return {"summary": summary, "lessons": lessons}
