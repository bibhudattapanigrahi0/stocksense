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
    actors = list(dict.fromkeys(c for c in (buys if net > 0 else sells)["client"] if c))[:3]
    n = int((buys if net > 0 else sells).shape[0])
    verb = "bought" if net > 0 else "sold"
    who = (actors[0][:28] + ("…" if actors and len(actors[0]) > 28 else "")) if actors else "large traders"
    return {
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


def live_signal(symbol: str) -> Optional[dict]:
    """Live NSE bulk/block-deal footprint for an IN symbol (smart_money contract)."""
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
