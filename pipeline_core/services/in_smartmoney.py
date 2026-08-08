"""
India "smart money" — NSE bulk & block deals (large, disclosed institutional-scale trades).

India has no 13F, but SEBI requires any trade moving ≥0.5% of a company's equity in a day to be
disclosed the same evening as a BULK/BLOCK deal, with the buyer/seller CLIENT NAME. A fresh
disclosed BUY is a real coattail footprint: someone with size stepped in and had to tell the market.

This wraps the project's existing NSE fetch (services/nse_data.get_bulk_deals — a cookie-primed
session that degrades to [] when NSE blocks the request, which is common from servers). So this source
lights up wherever NSE is reachable (e.g. an India deployment) and otherwise stays silent, in which
case IN equities fall back to the yfinance ownership baseline in smart_money.get_signal.

Implements the smart_money source-module contract (live_signal / series / enrich_frames). Bulk deals
are point events dated EOD, so the series is indexed by the NEXT day (availability) — no lookahead.
Named ace-investor matching (Jhunjhunwala/Kacholia/…) is a deliberate later add (sm_in_ace_enabled);
for now every disclosed large buyer counts as a smart-money footprint. Degrades to None/no-op; never raises.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from pipeline_core.config import settings

logger = logging.getLogger(__name__)

IN_RECENT_DAYS = 45           # a bulk-deal buy this recent counts as a live accumulation footprint


def _num(x) -> float:
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _side(deal_type) -> Optional[str]:
    s = str(deal_type or "").strip().upper()[:1]
    return "buy" if s == "B" else "sell" if s == "S" else None


def _fetch_deals(symbol: str) -> list[dict]:
    """Network leaf (patched out in tests): recent NSE bulk/block deals for a symbol."""
    from pipeline_core.services import nse_data
    from pipeline_core.markets import display_ticker
    return nse_data.get_bulk_deals(display_ticker(symbol))


def parse_deals(deals: list[dict]) -> Optional[pd.DataFrame]:
    """Raw NSE deals → tidy DataFrame [date, client, side, qty]. Pure — unit-tested directly."""
    if not deals:
        return None
    rows = []
    for d in deals:
        side = _side(d.get("deal_type"))
        dt = pd.to_datetime(d.get("date"), errors="coerce", dayfirst=True)
        qty = _num(d.get("quantity"))
        if side is None or pd.isna(dt) or qty <= 0:
            continue
        rows.append({"date": dt, "client": d.get("client"), "side": side, "qty": qty})
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("date")


def series(symbol: str) -> Optional[pd.DataFrame]:
    """Per-day net bulk-deal buy quantity, indexed by AVAILABILITY date (deal date + 1 day, since
    deals are disclosed after the close). Column `bulk_net_qty` (buys − sells). None if no deals."""
    df = parse_deals(_fetch_deals(symbol))
    if df is None or df.empty:
        return None
    signed = df.assign(signed=df["qty"].where(df["side"] == "buy", -df["qty"]))
    daily = signed.groupby("date")["signed"].sum()
    out = pd.DataFrame({"bulk_net_qty": daily.values},
                       index=pd.DatetimeIndex(daily.index) + pd.Timedelta(days=1))
    return out[~out.index.duplicated(keep="last")].sort_index()


# ── Ace-investor name matching (flag-gated: sm_in_ace_enabled) ───────
# Bulk-deal client names are disclosed, so a curated list of India's marquee investors lets us turn
# "a large buyer stepped in" into "Mukul Agrawal bought". Substring match on the uppercased client name
# (best-effort — a curated list needs maintenance, hence default-off). This is the non-fragile half of
# the ace signal; the deeper quarterly shareholding-pattern scrape (>1% holdings) is still a TODO.
ACE_INVESTORS: dict[str, str] = {
    "JHUNJHUNWALA": "Jhunjhunwala (RARE Enterprises)",
    "KACHOLIA": "Ashish Kacholia",
    "MUKUL": "Mukul Agrawal",
    "KEDIA": "Vijay Kedia",
    "DOLLY KHANNA": "Dolly Khanna",
    "DAMANI": "RK Damani",
    "KELA": "Madhusudan Kela",
    "SINGHANIA": "Sunil Singhania (Abakkus)",
    "GOEL": "Anil Kumar Goel",
}


def _match_ace(client) -> Optional[str]:
    c = str(client or "").upper()
    for frag, name in ACE_INVESTORS.items():
        if frag in c:
            return name
    return None


def _annotate_aces(sig: dict, side_df, direction: str) -> dict:
    """When sm_in_ace_enabled, upgrade a bulk-deal signal that involves a known ace investor — name
    them, raise conviction, and relabel. No-op when the flag is off or no ace matches."""
    if not getattr(settings, "sm_in_ace_enabled", False):
        return sig
    aces = list(dict.fromkeys(n for n in (_match_ace(c) for c in side_df["client"]) if n))
    if not aces:
        return sig
    verb = "bought" if direction == "accumulate" else "sold"
    out = dict(sig)
    out.update({
        "source": "nse-ace-investors",
        "label": "Ace investor",
        "ace": True,
        "actors": aces + [a for a in sig.get("actors", []) if a not in aces],
        "n_actors": len(aces),
        "conviction": "high" if direction == "accumulate" else sig.get("conviction"),
        "detail": f"Ace investor {verb}: " + ", ".join(aces[:2])
                  + (f" +{len(aces) - 2} more" if len(aces) > 2 else ""),
    })
    return out


def signal_from_deals(df: Optional[pd.DataFrame], asof: Optional[pd.Timestamp] = None) -> Optional[dict]:
    """Recent tidy deals → normalized smart-money signal (accumulate on a recent net buy). Pure."""
    if df is None or df.empty:
        return None
    asof = asof or df["date"].max()
    recent = df[df["date"] >= asof - pd.Timedelta(days=IN_RECENT_DAYS)]
    if recent.empty:
        return None
    buys = recent[recent["side"] == "buy"]
    sells = recent[recent["side"] == "sell"]
    net = buys["qty"].sum() - sells["qty"].sum()
    if net == 0:
        return None
    direction = "accumulate" if net > 0 else "distribute"
    side_df = buys if net > 0 else sells
    actors = list(dict.fromkeys(c for c in side_df["client"] if c))[:3]
    n = int(side_df.shape[0])
    verb = "bought" if net > 0 else "sold"
    who = (actors[0][:28] + ("…" if actors and len(actors[0]) > 28 else "")) if actors else "large traders"
    sig = {
        "available": True,
        "source": "nse-bulk-deals",
        "family": "bulk_deal",
        "direction": direction,
        "net_delta_pct": None,
        "conviction": ("high" if len(actors) >= 2 else "medium") if direction == "accumulate" else None,
        "actors": actors,
        "n_actors": len(actors),
        "label": "Bulk deal",
        "detail": f"{n} disclosed bulk-deal {verb[:-1]}(s) in {IN_RECENT_DAYS}d — {who}"
                  + (f" +{len(actors) - 1} more" if len(actors) > 1 else ""),
        "as_of": (df["date"].max()).isoformat(),
    }
    return _annotate_aces(sig, side_df, direction)


# ── Ace-investor HOLDINGS from the quarterly shareholding pattern (flag-gated) ──
# The deeper ace signal: a named investor holding >1% of a company is disclosed each quarter in the
# shareholding pattern — a standing conviction footprint, stronger than a single bulk deal. The MATCHER
# below is complete and tested; the live FETCH is best-effort — NSE's API surface shifts and blocks
# servers (endpoints currently 404/503 from many hosts), so it degrades to [] wherever it's unreachable
# and the signal simply doesn't fire. Point `_fetch_shareholders` at a working feed to activate it.

def _parse_shareholders(data) -> list[dict]:
    """Defensively extract [{name, pct}] named public shareholders from a shareholding-pattern payload.
    Tolerant of key naming since the exact NSE shape varies; unknown shapes ⇒ []. Pure."""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or r.get("shareholder") or r.get("holderName") or r.get("clientName")
        raw_pct = (r.get("pct") if r.get("pct") is not None else r.get("percentage")
                   if r.get("percentage") is not None else r.get("holding")
                   if r.get("holding") is not None else r.get("shareholding"))
        try:
            pct = float(str(raw_pct).replace("%", "").replace(",", "").strip()) if raw_pct is not None else None
        except (TypeError, ValueError):
            pct = None
        if name:
            out.append({"name": name, "pct": pct})
    return out


def ace_holdings_from_shareholders(shareholders: list[dict]) -> Optional[dict]:
    """Named >1% public shareholders → an ace-investor holdings signal, matching against ACE_INVESTORS.
    A standing ace holding is a bullish coattail footprint. None if no ace matches. Pure — unit-tested."""
    matches = []
    for sh in shareholders or []:
        name = _match_ace(sh.get("name"))
        if name:
            matches.append((name, sh.get("pct")))
    if not matches:
        return None
    matches.sort(key=lambda m: (m[1] or 0), reverse=True)
    names = list(dict.fromkeys(m[0] for m in matches))
    top_pct = matches[0][1]
    pct_txt = f" holds {top_pct:.1f}%" if top_pct is not None else " holds a >1% stake"
    return {
        "available": True, "source": "nse-shareholding", "family": "ace",
        "direction": "accumulate",          # a disclosed ace holding is a standing bullish footprint
        "net_delta_pct": None, "conviction": "high",
        "actors": names, "n_actors": len(names), "label": "Ace holding", "ace": True,
        "detail": f"{names[0]}{pct_txt}" + (f" (+{len(names) - 1} more ace investor{'s' if len(names) > 2 else ''})"
                                            if len(names) > 1 else ""),
        "as_of": None,
    }


def _fetch_shareholders(symbol: str) -> list[dict]:
    """Best-effort named >1% shareholders from NSE's shareholding pattern (network leaf, patched in
    tests). Graceful: any non-200 / shift in NSE's API / parse issue ⇒ []. Swap in a working feed
    (an NSE-reachable proxy, a cached export, or a data vendor) to activate the ace-holdings signal."""
    try:
        from pipeline_core.markets import display_ticker
        from pipeline_core.services import nse_data
        session = nse_data._nse_session()
        url = f"{nse_data.NSE_BASE}/api/corporate-shareholding?symbol={display_ticker(symbol)}"
        resp = session.get(url, timeout=12)
        if resp.status_code != 200:
            return []
        return _parse_shareholders(resp.json())
    except Exception as e:
        logger.warning("Shareholding fetch failed for %s: %s", symbol, e)
        return []


def live_signal(symbol: str) -> Optional[dict]:
    """Live NSE smart-money footprint for an IN symbol (smart_money contract). When ace tracking is on,
    a disclosed ace HOLDING (>1% in the shareholding pattern) is the strongest signal and wins; otherwise
    fall back to bulk/block-deal activity (which itself names an ace when one appears on a deal)."""
    if getattr(settings, "sm_in_ace_enabled", False):
        holdings = ace_holdings_from_shareholders(_fetch_shareholders(symbol))
        if holdings:
            return holdings
    return signal_from_deals(parse_deals(_fetch_deals(symbol)))


def enrich_frames(df_by_ticker: dict) -> None:
    """Merge `bulk_net_qty` onto each frame at the deal's availability date (0 elsewhere — bulk deals
    are discrete events, not a state to forward-fill). Mutates in place; per-symbol failures isolated."""
    if not getattr(settings, "sm_in_bulk_deals_enabled", False):
        return
    for tk, df in df_by_ticker.items():
        try:
            s = series(tk)
            if s is None or s.empty:
                continue
            idx = s.index.tz_localize(df.index.tz) if getattr(df.index, "tz", None) is not None else s.index
            col = pd.Series(s["bulk_net_qty"].values, index=idx)
            df["bulk_net_qty"] = col.reindex(df.index, fill_value=0.0)
        except Exception as e:      # pragma: no cover — defensive
            logger.warning("Bulk-deal enrichment skipped for %s: %s", tk, e)
