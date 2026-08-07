"""
CFTC Commitments of Traders (COT) — the metals market's "smart money".

Price-only markets have no 13F filings, but the CFTC's weekly COT report is the canonical read
on who is positioned where in the futures: the COMMERCIALS (producers, refiners, hedgers — the
informed money that actually handles the physical metal) versus large NON-commercial speculators.
Commercials are contrarian and tend to be right at extremes, so "follow the commercials" — turn
bullish when they are unusually net-long for their own recent range — is a classic coattail signal.

We read the free, keyless CFTC Socrata dataset (legacy futures-only, `6dca-aqww`). Everything is
disclosure-dated: each weekly report snapshots the prior *Tuesday* and is released the following
*Friday*, so the series is indexed by (report_date + release lag) and forward-filled — a backtest
can never see a report before it was public. The strength read is the classic COT INDEX: the
commercial net position normalized to 0–100 over a multi-year range (100 = most net-long they've
been, i.e. most bullish).

Implements the smart_money source-module contract — `live_signal(symbol)`, `series(symbol)`,
`enrich_frames(df_by_ticker)` — so services/smart_money.py lights METALS up automatically.
Degrades to None / no-op on any failure (network, unknown symbol); never raises into the caller.
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Optional

import diskcache
import pandas as pd
import requests

from pipeline_core.config import CACHE_DIR, settings

logger = logging.getLogger(__name__)
cache = diskcache.Cache(str(CACHE_DIR / "cot"))

COT_DATASET = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
COT_TTL = 24 * 3600            # weekly data — a daily refresh is plenty
COT_RELEASE_LAG_DAYS = 3       # Tuesday snapshot released the following Friday (anti-lookahead)
_HTTP_TIMEOUT = 20

# Metal symbol → the unambiguous CFTC `market_and_exchange_names` for its underlying future.
# ETFs and miners map to the metal they track (a soft proxy — COT gold positioning informs GLD/GDX).
_MARKET_BY_SYMBOL: dict[str, str] = {
    "GC=F": "GOLD - COMMODITY EXCHANGE INC.", "GLD": "GOLD - COMMODITY EXCHANGE INC.",
    "IAU": "GOLD - COMMODITY EXCHANGE INC.", "GDX": "GOLD - COMMODITY EXCHANGE INC.",
    "SI=F": "SILVER - COMMODITY EXCHANGE INC.", "SLV": "SILVER - COMMODITY EXCHANGE INC.",
    "PL=F": "PLATINUM - NEW YORK MERCANTILE EXCHANGE", "PPLT": "PLATINUM - NEW YORK MERCANTILE EXCHANGE",
    "PA=F": "PALLADIUM - NEW YORK MERCANTILE EXCHANGE", "PALL": "PALLADIUM - NEW YORK MERCANTILE EXCHANGE",
    "HG=F": "COPPER- #1 - COMMODITY EXCHANGE INC.", "CPER": "COPPER- #1 - COMMODITY EXCHANGE INC.",
}

_FIELDS = [
    "report_date_as_yyyy_mm_dd",
    "comm_positions_long_all", "comm_positions_short_all",
    "noncomm_positions_long_all", "noncomm_positions_short_all",
    "open_interest_all",
]


def _ua() -> str:
    return getattr(settings, "sec_user_agent", None) or "StockSense/1.0 (contact: set sec_user_agent)"


def _fetch_rows(market_name: str) -> list[dict]:
    """Raw weekly COT rows for one CFTC market (ascending by date), disk-cached. Network."""
    key = f"cot:{market_name}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    params = {
        "$where": f"market_and_exchange_names='{market_name}'",
        "$select": ",".join(_FIELDS),
        "$order": "report_date_as_yyyy_mm_dd ASC",
        "$limit": 5000,
    }
    resp = requests.get(COT_DATASET, params=params, headers={"User-Agent": _ua()}, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    rows = resp.json()
    cache.set(key, rows, expire=COT_TTL)
    return rows


def compute_cot_frame(rows: list[dict], lookback_weeks: Optional[int] = None,
                      lag_days: int = COT_RELEASE_LAG_DAYS) -> Optional[pd.DataFrame]:
    """Raw COT rows → DataFrame indexed by AVAILABILITY date (report_date + release lag) with
    `cot_comm_net` (commercial net position) and `cot_index` (that net position normalized to
    0–100 over the lookback range). Pure — unit-tested directly."""
    if not rows:
        return None
    lb = lookback_weeks or getattr(settings, "cot_lookback_weeks", 156)
    df = pd.DataFrame(rows)
    for c in _FIELDS[1:]:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df["report_date"] = pd.to_datetime(df.get("report_date_as_yyyy_mm_dd"), errors="coerce")
    df = df.dropna(subset=["report_date"]).sort_values("report_date")
    if df.empty:
        return None
    avail = df["report_date"] + pd.Timedelta(days=lag_days)
    comm_net = (df["comm_positions_long_all"] - df["comm_positions_short_all"]).astype(float)
    out = pd.DataFrame({"cot_comm_net": comm_net.values}, index=pd.DatetimeIndex(avail.values))
    out = out[~out.index.duplicated(keep="last")].sort_index()
    s = out["cot_comm_net"]
    mp = min(26, lb)
    lo = s.rolling(lb, min_periods=mp).min()
    hi = s.rolling(lb, min_periods=mp).max()
    rng = (hi - lo).replace(0, pd.NA)
    out["cot_index"] = ((s - lo) / rng * 100).astype(float)
    return out


def series(symbol: str) -> Optional[pd.DataFrame]:
    """COT series (disclosure-dated) for a metal symbol, or None if unmapped / unavailable."""
    market_name = _MARKET_BY_SYMBOL.get(symbol.strip().upper())
    if not market_name:
        return None
    try:
        rows = _fetch_rows(market_name)
    except Exception as e:
        logger.warning("COT fetch failed for %s (%s): %s", symbol, market_name, e)
        return None
    return compute_cot_frame(rows)


def _conviction(cot_index: float, high: float) -> Optional[str]:
    if cot_index >= max(90.0, high):
        return "high"
    if cot_index >= high:
        return "medium"
    return None


def signal_from_frame(df: Optional[pd.DataFrame]) -> Optional[dict]:
    """Latest COT reading → normalized smart-money signal, or None. Pure — unit-tested directly."""
    if df is None or df.empty or "cot_index" not in df or pd.isna(df["cot_index"].iloc[-1]):
        return None
    idx = float(df["cot_index"].iloc[-1])
    high = float(getattr(settings, "cot_index_high", 75.0))
    low = float(getattr(settings, "cot_index_low", 25.0))
    direction = "accumulate" if idx >= high else "distribute" if idx <= low else "neutral"
    stance = ("Commercials heavily net-long" if direction == "accumulate"
              else "Commercials heavily net-short" if direction == "distribute"
              else "Commercials mid-range")
    return {
        "available": True,
        "source": "cftc-cot",
        "family": "cot",
        "direction": direction,
        "net_delta_pct": round(idx - 50.0, 1),      # signed tilt around neutral (bullish > 0)
        "cot_index": round(idx, 1),
        "conviction": _conviction(idx, high),
        "actors": ["Commercials"],
        "n_actors": 1,
        "label": "COT",
        "detail": f"{stance} — COT index {idx:.0f}/100 (3y range)",
        "as_of": df.index[-1].tz_localize(timezone.utc).isoformat() if df.index[-1].tzinfo is None
                 else df.index[-1].isoformat(),
    }


def live_signal(symbol: str) -> Optional[dict]:
    """The live COT overlay for a metal symbol (smart_money source-module contract)."""
    return signal_from_frame(series(symbol))


def _ffill_onto(src: pd.Series, target_index) -> pd.Series:
    """Forward-fill a (weekly, tz-naive) COT column onto a daily price index, tolerating a tz-aware
    price index (yfinance frames can carry an exchange tz) — align on tz-naive dates, then restore
    the frame's own index so the assignment lines up."""
    tgt_naive = target_index.tz_localize(None) if getattr(target_index, "tz", None) is not None else target_index
    s = src
    if getattr(s.index, "tz", None) is not None:
        s = s.copy()
        s.index = s.index.tz_localize(None)
    aligned = s.sort_index().reindex(tgt_naive, method="ffill")
    aligned.index = target_index
    return aligned


def enrich_frames(df_by_ticker: dict) -> None:
    """Merge `cot_index` / `cot_comm_net` onto each mapped metal frame (forward-filled from the
    release date). Mutates in place; skips unmapped symbols; per-symbol failures are isolated."""
    merged = 0
    for tk, df in df_by_ticker.items():
        try:
            s = series(tk)
            if s is None or s.empty:
                continue
            df["cot_index"] = _ffill_onto(s["cot_index"], df.index)
            df["cot_comm_net"] = _ffill_onto(s["cot_comm_net"], df.index)
            merged += 1
        except Exception as e:      # pragma: no cover — defensive
            logger.warning("COT enrichment skipped for %s: %s", tk, e)
    if merged:
        logger.info("COT enrichment: merged positioning onto %d/%d metal frames", merged, len(df_by_ticker))
