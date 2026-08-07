"""
Correlation-aware idea de-duplication.

A ranked idea book can be dominated by names that all move together — in crypto especially, a
dozen "independent" long ideas are often one bet on BTC repeated a dozen times, so when BTC turns
they all stop out at once. This collapses each correlated cluster down to its single best-ranked
idea *before* sizing, so conviction and position size reflect **independent** setups rather than
one crowded trade wearing many tickers.

Pure functions over price frames + a rank-sorted book; no I/O. Driven by the OHLCV the caller has
already loaded, so de-dup costs nothing extra on the intraday path.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from pipeline_core.config import settings

logger = logging.getLogger(__name__)


def _to_naive_utc(s: pd.Series) -> pd.Series:
    """Normalise a series' DatetimeIndex to tz-naive UTC so frames from different sources can be
    aligned. Crypto OHLCV (Binance) is tz-naive UTC while the yfinance fallback is tz-aware — mixing
    the two in one DataFrame raises "Cannot join tz-naive with tz-aware". Leaves non-datetime or
    already-naive indexes untouched."""
    idx = s.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        s = s.copy()
        s.index = idx.tz_convert("UTC").tz_localize(None)
    return s


def _returns_matrix(frames: dict, lookback: int) -> Optional[pd.DataFrame]:
    """A columns=symbols frame of trailing close-to-close returns, aligned on timestamp. None if
    fewer than two symbols carry enough data to correlate."""
    cols: dict[str, pd.Series] = {}
    for sym, df in frames.items():
        if df is None or getattr(df, "empty", True) or "Close" not in df:
            continue
        r = _to_naive_utc(df["Close"].astype(float).pct_change().tail(lookback))
        if r.notna().sum() >= 5:
            cols[sym] = r
    if len(cols) < 2:
        return None
    return pd.DataFrame(cols)


def cluster_symbols(
    frames: dict,
    *,
    threshold: Optional[float] = None,
    lookback: int = 100,
    enabled: Optional[bool] = None,
) -> dict:
    """Map each symbol → a cluster id, greedily grouping symbols whose trailing-return correlation
    ≥ `threshold`. Returns {} when disabled or when fewer than two usable series exist (→ caller
    does no de-dup). Unset args fall back to settings."""
    on = settings.dedup_enabled if enabled is None else enabled
    thr = settings.dedup_corr_threshold if threshold is None else threshold
    if not on:
        return {}
    # De-dup is a ranking nicety, never worth failing the whole board over — degrade to "no de-dup"
    # if the frames can't be correlated (e.g. malformed/misaligned indexes from a fallback source).
    try:
        mat = _returns_matrix(frames, lookback)
        if mat is None:
            return {}
        corr = mat.corr()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"dedup correlation skipped ({type(e).__name__}: {e})")
        return {}
    symbols = list(corr.columns)
    cluster_of: dict[str, int] = {}
    nid = 0
    for s in symbols:  # greedy single-pass: each unclustered symbol seeds a cluster and pulls in peers
        if s in cluster_of:
            continue
        cluster_of[s] = nid
        for t in symbols:
            if t == s or t in cluster_of:
                continue
            c = corr.at[s, t]
            if pd.notna(c) and float(c) >= thr:
                cluster_of[t] = nid
        nid += 1
    return cluster_of


def dedup_book(ideas: list, clusters: dict, *, keep_per_cluster: int = 1) -> tuple[list, dict]:
    """Keep the top `keep_per_cluster` ideas per correlated cluster (book must already be
    rank-sorted best-first); drop the rest as correlated duplicates. Annotates each kept idea with
    `cluster` (id) and `cluster_peers` (how many book ideas share its cluster). Returns
    (kept, summary). Empty `clusters` → no-op (everything kept)."""
    if not clusters:
        return ideas, {"kept": len(ideas), "dropped": 0, "collapsed_clusters": 0}

    members: dict[int, int] = {}
    for idea in ideas:
        cid = clusters.get(idea.get("ticker"))
        if cid is not None:
            members[cid] = members.get(cid, 0) + 1

    seen: dict[int, int] = {}
    kept: list = []
    dropped = 0
    for idea in ideas:
        cid = clusters.get(idea.get("ticker"))
        if cid is None:  # unknown symbol → never dropped
            kept.append(idea)
            continue
        if seen.get(cid, 0) < keep_per_cluster:
            idea["cluster"] = cid
            idea["cluster_peers"] = members.get(cid, 1)
            seen[cid] = seen.get(cid, 0) + 1
            kept.append(idea)
        else:
            dropped += 1

    collapsed = sum(1 for m in members.values() if m > keep_per_cluster)
    return kept, {"kept": len(kept), "dropped": dropped, "collapsed_clusters": collapsed}
