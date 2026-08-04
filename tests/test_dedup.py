"""Correlation-aware de-duplication — cluster symbols by trailing-return correlation, then
collapse each cluster to its best-ranked idea. Pure, network-free."""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline_core import dedup


def _frame(closes) -> pd.DataFrame:
    c = np.asarray(closes, float)
    idx = pd.date_range("2026-07-01", periods=len(c), freq="15min")
    return pd.DataFrame(
        {"Open": c, "High": c * 1.001, "Low": c * 0.999, "Close": c, "Volume": np.full(len(c), 1e6)},
        index=idx,
    )


# ── clustering ──────────────────────────────────────────────────────


def test_correlated_symbols_share_cluster_independent_apart():
    base = np.cumsum(np.random.default_rng(0).normal(0, 1, 200)) + 500.0
    a = _frame(base)
    b = _frame(base * 1.01)                                              # affine of A → returns ≈ identical
    c = _frame(np.cumsum(np.random.default_rng(9).normal(0, 1, 200)) + 300.0)  # independent walk
    clusters = dedup.cluster_symbols({"A": a, "B": b, "C": c}, threshold=0.8)
    assert clusters["A"] == clusters["B"]      # highly correlated → same cluster
    assert clusters["C"] != clusters["A"]      # independent → its own cluster


def test_disabled_returns_no_clusters():
    a = _frame(np.linspace(100, 110, 60))
    b = _frame(np.linspace(100, 110, 60))
    assert dedup.cluster_symbols({"A": a, "B": b}, enabled=False) == {}


def test_too_few_usable_series_no_clusters():
    a = _frame(np.linspace(100, 110, 60))
    assert dedup.cluster_symbols({"A": a}, threshold=0.8) == {}
    assert dedup.cluster_symbols({}, threshold=0.8) == {}


# ── book collapse ───────────────────────────────────────────────────


def test_dedup_book_keeps_best_ranked_per_cluster():
    ideas = [{"ticker": t} for t in ("A", "B", "C", "D")]     # already rank-sorted A>B>C>D
    clusters = {"A": 0, "B": 0, "D": 0, "C": 1}               # A,B,D correlated; C alone
    kept, summ = dedup.dedup_book(ideas, clusters)
    assert [i["ticker"] for i in kept] == ["A", "C"]         # best of cluster 0 (A) + singleton C
    assert summ["dropped"] == 2 and summ["collapsed_clusters"] == 1
    assert kept[0]["cluster"] == 0 and kept[0]["cluster_peers"] == 3   # A stands in for 3


def test_dedup_book_noop_without_clusters():
    ideas = [{"ticker": "A"}, {"ticker": "B"}]
    kept, summ = dedup.dedup_book(ideas, {})
    assert kept is ideas and summ["dropped"] == 0 and summ["collapsed_clusters"] == 0


def test_dedup_book_unknown_symbols_never_dropped():
    ideas = [{"ticker": "A"}, {"ticker": "Z"}]               # Z not in the cluster map
    kept, summ = dedup.dedup_book(ideas, {"A": 0})
    assert [i["ticker"] for i in kept] == ["A", "Z"] and summ["dropped"] == 0


def test_keep_two_per_cluster():
    ideas = [{"ticker": t} for t in ("A", "B", "C")]
    kept, summ = dedup.dedup_book(ideas, {"A": 0, "B": 0, "C": 0}, keep_per_cluster=2)
    assert [i["ticker"] for i in kept] == ["A", "B"] and summ["dropped"] == 1
