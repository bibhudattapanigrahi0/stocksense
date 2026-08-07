"""
Alert engine — turns state changes into notifications the user sees in-app.

Runs on the same background auto-refresh loop that keeps the Trade Ideas board fresh (app.py
`_auto_refresh_loop`), so alerts surface even when nobody is looking. Every alert is persisted
(`run_store.save_alert`) and de-duplicated by a stable fingerprint, so the same condition isn't
re-notified on every pass. Delivery is in-app only (no external integrations): the UI polls
`GET /api/v1/alerts` and raises a bell badge + browser notification.

Triggers:
  • idea_closed — a tracked idea hit its stop / target / time-stop (from the trade ledger)
  • watch_level — a watchlist ticker reached a user-set price level (above / below)
  • new_idea    — a fresh idea appeared for a watchlist ticker

`build_digest` rolls the last 24h up into a morning brief (ideas closed & win count, alert
counts by type, open ideas).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pipeline_core.config import settings
from pipeline_core.services import market_data, run_store

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse(iso: Optional[str]) -> Optional[datetime]:
    """Parse an ISO datetime or a date-only string into an aware UTC datetime."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _within(iso: Optional[str], cutoff: datetime) -> bool:
    dt = _parse(iso)
    return dt is not None and dt >= cutoff


def _alert(market: str, type_: str, title: str, body: str, *, ticker: Optional[str] = None,
           severity: str = "info", fingerprint: Optional[str] = None, data: Optional[dict] = None) -> dict:
    return {
        "market": (market or "IN").upper(), "type": type_, "title": title, "body": body,
        "ticker": ticker, "severity": severity, "fingerprint": fingerprint,
        "data": data or {}, "created_at": _now().isoformat(), "read": False,
    }


async def evaluate_market(market: str, *, recent_hours: int = 48, user_id: Optional[str] = None) -> dict:
    """Evaluate every alert trigger for one market and persist new alerts. Idempotent — alerts are
    de-duplicated by fingerprint, so repeated calls don't re-notify. Returns {emitted, by_type}.
    Scoped to `user_id`'s watchlist/alerts (the owner when None — the background loop)."""
    from pipeline_core.backtest import runner  # lazy: runner pulls in heavy deps

    market = (market or "IN").upper()
    emitted: list[dict] = []

    async def emit(alert: dict) -> None:
        saved = await run_store.save_alert(alert, user_id=user_id)
        if saved:
            emitted.append(saved)

    watch = await run_store.list_watch(market, user_id=user_id)
    watch_tickers = {w["ticker"] for w in watch}

    # 1) Watchlist price-level reached (first time per level per day; fingerprint suppresses repeats).
    for w in watch:
        above, below = w.get("level_above"), w.get("level_below")
        if above is None and below is None:
            continue
        try:
            px = await asyncio.to_thread(market_data.get_quote, w["ticker"], market)
        except Exception as e:
            logger.debug("alerts: quote failed for %s: %s", w["ticker"], e)
            px = None
        if px is None:
            continue
        if above is not None and px >= above:
            await emit(_alert(market, "watch_level", f"{w['ticker']} reached {above}",
                              f"{w['ticker']} is at {round(px, 2)}, at/above your {above} level.",
                              ticker=w["ticker"], severity="warn",
                              fingerprint=f"level|{market}|{w['ticker']}|above|{above}|{_day()}",
                              data={"price": px, "level": above, "dir": "above"}))
        if below is not None and px <= below:
            await emit(_alert(market, "watch_level", f"{w['ticker']} reached {below}",
                              f"{w['ticker']} is at {round(px, 2)}, at/below your {below} level.",
                              ticker=w["ticker"], severity="warn",
                              fingerprint=f"level|{market}|{w['ticker']}|below|{below}|{_day()}",
                              data={"price": px, "level": below, "dir": "below"}))

    # 2) Tracked ideas that closed recently (stop / target / time-stop) — from the ledger.
    try:
        status = await runner.tracked_ideas_status(market)
    except Exception as e:
        logger.warning("alerts: tracked status failed for %s: %s", market, e)
        status = {"closed": [], "open": []}
    cutoff = _now() - timedelta(hours=recent_hours)
    for c in status.get("closed", []):
        if not _within(c.get("closed_at") or c.get("exit_date"), cutoff):
            continue
        reason = (c.get("close_reason") or c.get("exit_reason") or "closed").lower()
        ret = c.get("realized_return_pct")
        if ret is None:
            ret = c.get("return_pct")
        won = (ret or 0) > 0
        verb = {"target": "hit its target", "stop": "hit its stop", "time": "timed out"}.get(reason, "closed")
        tail = f" ({'+' if won else ''}{ret}%)" if ret is not None else ""
        await emit(_alert(market, "idea_closed", f"{c['ticker']} {verb}",
                          f"Tracked idea {c['ticker']} {verb}{tail}.",
                          ticker=c["ticker"], severity=("good" if won else "bad"),
                          fingerprint=f"closed|{market}|{c['ticker']}|{c.get('exit_date')}|{reason}",
                          data={"reason": reason, "return_pct": ret}))

    # 2b) Hold-coaching: an open position dropped BEYOND its normal-dip band (winners rarely fell this
    #     far before recovering) → an anti-panic "review the thesis" nudge. Reuses tracked_ideas_status'
    #     dd_status (already computed above), so no extra I/O. One alert per ticker per day.
    if settings.alert_hold_coach_enabled:
        for o in status.get("open", []):
            if o.get("dd_status") != "beyond":
                continue
            dd, typ = o.get("dd_pct"), o.get("typical_drawdown_pct")
            tail = f" (−{dd}% vs ~{typ}% typical)" if dd is not None and typ is not None else ""
            await emit(_alert(market, "hold_coach", f"{o['ticker']} beyond its normal dip",
                              f"{o['ticker']} is down past where winning trades usually recover{tail} — "
                              "review the thesis before bailing.",
                              ticker=o["ticker"], severity="warn",
                              fingerprint=f"holdcoach|{market}|{o['ticker']}|{_day()}",
                              data={"dd_pct": dd, "typical": typ, "deep": o.get("deep_drawdown_pct")}))

    # 3) Fresh ideas for watchlist tickers only (targeted, not the whole board).
    if watch_tickers:
        try:
            ideas = runner.horizon_ideas(market=market)
        except Exception as e:
            logger.debug("alerts: horizon_ideas failed for %s: %s", market, e)
            ideas = None
        if ideas:
            for h in ideas.get("horizons", []):
                for c in ideas["buckets"][h]["candidates"]:
                    if c["ticker"] not in watch_tickers or not c.get("is_new"):
                        continue
                    await emit(_alert(market, "new_idea", f"New {h}-term idea: {c['ticker']}",
                                      f"{c['ticker']} is a fresh {h}-term idea "
                                      f"(conviction {c.get('conviction')}, ~{c.get('avg_win_rate')}% win).",
                                      ticker=c["ticker"], severity="info",
                                      fingerprint=f"newidea|{market}|{c['ticker']}|{h}|{c.get('latest_signal_date')}",
                                      data={"horizon": h, "conviction": c.get("conviction"),
                                            "risk_pct": c.get("risk_pct"), "alloc_pct": c.get("alloc_pct")}))

            # 3b) A watchlist idea is IN ITS BUY ZONE right now — a "triggered, consider entering" nudge
            #     (take-the-trade side). Targeted to the watchlist, so at most a few live quotes.
            if settings.alert_triggered_enabled:
                for h in ideas.get("horizons", []):
                    for c in ideas["buckets"][h]["candidates"]:
                        if c["ticker"] not in watch_tickers:
                            continue
                        try:
                            px = await asyncio.to_thread(market_data.get_quote, c["ticker"], market)
                        except Exception:
                            px = None
                        if px is None or runner._idea_zone(px, c) != "in_zone":
                            continue
                        await emit(_alert(market, "idea_triggered", f"{c['ticker']} entered its buy zone",
                                          f"{c['ticker']} is at {round(px, 2)}, inside its {h}-term buy zone "
                                          f"(conviction {c.get('conviction')}) — triggered, consider entering.",
                                          ticker=c["ticker"], severity="info",
                                          fingerprint=f"triggered|{market}|{c['ticker']}|{h}|{_day()}",
                                          data={"horizon": h, "price": px, "conviction": c.get("conviction")}))

    # 4) The committed Hold Basket needs a MATERIAL rebalance (a holding broke, or a much-stronger idea
    #    appeared). One alert per distinct change-set per day (fingerprint) — no re-nagging.
    if settings.hold_basket_enabled:
        try:
            hb = await runner.hold_basket_status(market)
        except Exception as e:
            logger.debug("alerts: hold status failed for %s: %s", market, e)
            hb = None
        if hb and hb.get("committed") and hb.get("material"):
            changes = [a for a in hb.get("actions", []) if a["action"] in ("EXIT", "ADD", "BUY")]
            exits = sorted(a["ticker"] for a in changes if a["action"] == "EXIT")
            adds = sorted(a["ticker"] for a in changes if a["action"] in ("ADD", "BUY"))
            body = "; ".join(f"{a['ticker']} — {a['reason']}" for a in changes[:3])
            await emit(_alert(market, "hold_rebalance",
                              f"Hold Basket: {len(changes)} change{'s' if len(changes) != 1 else ''} to review",
                              body + ("." if body else ""), severity="warn",
                              fingerprint=f"holdreb|{market}|{'+'.join(exits)}/{'+'.join(adds)}|{_day()}",
                              data={"counts": hb.get("counts"), "actions": changes}))

    by_type: dict[str, int] = {}
    for a in emitted:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    return {"market": market, "emitted": len(emitted), "by_type": by_type, "alerts": emitted}


async def build_digest(market: str, *, hours: int = 24, user_id: Optional[str] = None) -> dict:
    """Roll the last `hours` up into a morning brief for one market (scoped to `user_id`)."""
    from pipeline_core.backtest import runner

    market = (market or "IN").upper()
    since = _now() - timedelta(hours=hours)

    alerts = await run_store.list_alerts(market, limit=200, user_id=user_id)
    recent = [a for a in alerts if _within(a.get("created_at"), since)]
    by_type: dict[str, int] = {}
    for a in recent:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1

    try:
        status = await runner.tracked_ideas_status(market)
    except Exception:
        status = {"open": [], "closed": []}
    closed_recent = [c for c in status.get("closed", [])
                     if _within(c.get("closed_at") or c.get("exit_date"), since)]

    def _ret(c):
        r = c.get("realized_return_pct")
        return c.get("return_pct") if r is None else r

    wins = sum(1 for c in closed_recent if (_ret(c) or 0) > 0)
    return {
        "market": market, "since": since.isoformat(), "generated_at": _now().isoformat(),
        "alert_count": len(recent), "by_type": by_type,
        "closed": {"total": len(closed_recent), "wins": wins, "losses": len(closed_recent) - wins},
        "open_ideas": len(status.get("open", [])),
        "highlights": recent[:10],
    }
