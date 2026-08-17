"""Equity oversold entry gate. The equity book is oversold mean-reversion; the closed ledger shows a
long entered EXTENDED (RSI >= ~50 / above its 50-DMA) realizing ~26% win vs ~75% for a genuinely
pulled-back entry — and re-simulation showed wider stops / trailing targets DON'T help (whipsaw
stop-outs are real losses). So an extended equity long is soft-demoted, reusing the cohort machinery."""
import pandas as pd
import pytest

from pipeline_core.backtest import runner
from pipeline_core.config import settings


def _frame(closes):
    return pd.DataFrame({"Close": [float(x) for x in closes]})


# ── _oversold_state classification ──────────────────────────────────

def test_falling_stock_is_oversold():
    ov = runner._oversold_state(_frame([160 - i for i in range(60)]))   # steady decline
    assert ov is not None and ov["oversold"] is True                    # low RSI AND below 50-DMA
    assert ov["rsi"] < settings.oversold_rsi_max and ov["vs_sma50_pct"] < 0


def test_rising_stock_is_extended():
    ov = runner._oversold_state(_frame([100 + i for i in range(60)]))   # steady advance
    assert ov is not None and ov["oversold"] is False                   # high RSI AND above 50-DMA
    assert ov["rsi"] >= settings.oversold_rsi_max and ov["vs_sma50_pct"] > 0


def test_pullback_below_50dma_counts_as_oversold():
    # Up for a long while, then a sharp recent dip that drops price below its 50-DMA → oversold.
    closes = [100 + i for i in range(50)] + [150 - 4 * i for i in range(1, 11)]
    ov = runner._oversold_state(_frame(closes))
    assert ov["vs_sma50_pct"] < 0 and ov["oversold"] is True


def test_thin_history_returns_none():
    assert runner._oversold_state(_frame([100, 101, 102])) is None      # < 50 bars → never fires blind
    assert runner._oversold_state(None) is None


# ── folding into the demote machinery (_recompose_bucket) ───────────

def _cand(ticker, **over):
    base = dict(ticker=ticker, side="long", conviction=3, entry_price=100.0, stop_price=94.0,
                target_price=110.0, stop_pct=0.06, target_pct=0.10, avg_win_rate=62.0, avg_sharpe=1.0)
    base.update(over)
    return base

def _bucket(cands):
    return {"horizon": "short", "candidates": cands}

def test_extended_equity_long_is_demoted_extended():
    b = _bucket([_cand("CLEAN"), _cand("EXT", not_oversold=True)])
    runner._recompose_bucket(b, 0.0, 10, market="US")
    tagged = {c["ticker"]: c.get("demote_reason") for c in b["candidates"]}
    assert tagged["EXT"] == "entered extended"          # flagged → demoted
    assert tagged["CLEAN"] is None                      # oversold → clean

def test_reasons_stack_with_cohort():
    b = _bucket([_cand("X", not_oversold=True)])
    runner._recompose_bucket(b, 0.0, 10, market="US")   # short horizon: no cohort reason
    assert b["candidates"][0]["demote_reason"] == "entered extended"
    b2 = {"horizon": "medium", "candidates": [_cand("Y", not_oversold=True)]}
    runner._recompose_bucket(b2, 0.0, 10, market="US")  # medium horizon IS a cohort
    assert b2["candidates"][0]["demote_reason"] == "medium horizon + entered extended"

def test_toggle_off_leaves_extended_untouched(monkeypatch):
    # With the gate off, enrich never sets not_oversold, so nothing is flagged. Simulate an unflagged
    # candidate: a clean idea is not demoted on a non-cohort horizon.
    b = _bucket([_cand("Z")])                           # no not_oversold flag
    runner._recompose_bucket(b, 0.0, 10, market="US")
    assert b["candidates"][0].get("demote_reason") is None
