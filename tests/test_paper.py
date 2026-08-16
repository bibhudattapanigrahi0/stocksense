"""Paper-trading account — position math, P&L, auto-close, attribution + persistence roundtrip.
Network-free (pure account math + tmp DB + mocked quotes)."""

from __future__ import annotations

import pytest

from pipeline_core import paper
from pipeline_core.services import run_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "paper.db")
    return tmp_path


# ── pure account math ───────────────────────────────────────────────


def test_open_by_alloc_pct_sizes_off_book_equity():
    acct = paper.new_account("in", 100_000.0)
    pos = paper.open_position(acct, "aaa", "long", 100.0, alloc_pct=10.0, stop=95, target=115, source="lab")
    assert pos["ticker"] == "AAA" and pos["side"] == "long" and pos["qty"] == 100.0   # 10% of 100k / 100
    assert pos["stop"] == 95 and pos["target"] == 115 and pos["source"] == "lab"
    assert acct["market"] == "IN" and len(acct["positions"]) == 1


@pytest.mark.parametrize("args", [("X", "long", 0.0), ("X", "sideways", 10.0)])
def test_open_invalid_inputs(args):
    with pytest.raises(ValueError):
        paper.open_position(paper.new_account("IN"), *args)


@pytest.mark.parametrize("alloc", [0.0, None])
def test_open_with_unsized_alloc_falls_back_to_default(alloc):
    # Regression: a watch-tier / speculative idea (e.g. a crypto short) carries alloc_pct=0.0; the paper
    # button forwarded it, so open_position computed qty=0 and raised "qty must be positive" — surfacing
    # as an error toast. A non-positive alloc must fall back to the default size, not error.
    acct = paper.new_account("CRYPTO", 100_000.0)
    pos = paper.open_position(acct, "PYTH", "short", 0.04, alloc_pct=alloc, stop=0.048, target=0.024)
    assert pos["side"] == "short" and pos["qty"] > 0
    assert round(pos["notional"], 2) == round(100_000.0 * paper.DEFAULT_ALLOC_PCT / 100.0, 2)  # 10% default


def test_open_zero_qty_rejected():
    with pytest.raises(ValueError):
        paper.open_position(paper.new_account("IN"), "X", "long", 10.0, qty=0)


def test_close_books_realized_pnl_and_removes_position():
    acct = paper.new_account("IN", 100_000.0)
    paper.open_position(acct, "AAA", "long", 100.0, qty=10)
    c = paper.close_position(acct, acct["positions"][0]["id"], 110.0, reason="target")
    assert c["pnl"] == 100.0 and c["return_pct"] == 10.0 and c["reason"] == "target"
    assert not acct["positions"] and len(acct["closed"]) == 1


def test_short_profits_when_price_falls():
    acct = paper.new_account("IN")
    paper.open_position(acct, "AAA", "short", 100.0, qty=10)
    c = paper.close_position(acct, acct["positions"][0]["id"], 90.0)
    assert c["pnl"] == 100.0 and c["return_pct"] == 10.0


def test_summary_equity_realized_unrealized_and_attribution():
    acct = paper.new_account("IN", 100_000.0)
    paper.open_position(acct, "AAA", "long", 100.0, qty=10, source="botX")     # stays open
    paper.open_position(acct, "BBB", "long", 50.0, qty=20, source="botY")
    paper.close_position(acct, acct["positions"][1]["id"], 60.0)               # +200 realized (botY)
    s = paper.summary(acct, {"AAA": 110.0})                                    # +100 unrealized (botX)
    assert s["realized_pnl"] == 200.0 and s["unrealized_pnl"] == 100.0
    assert s["equity"] == 100_300.0 and s["open_count"] == 1 and s["closed_count"] == 1
    assert any(a["source"] == "botY" and a["pnl"] == 200.0 for a in s["attribution"])


def test_auto_close_hits_stops_both_sides():
    acct = paper.new_account("IN")
    paper.open_position(acct, "AAA", "long", 100.0, qty=1, stop=95, target=110)
    paper.open_position(acct, "BBB", "short", 100.0, qty=1, stop=105, target=90)
    done = paper._auto_close_hits(acct, {"AAA": 94.0, "BBB": 106.0})           # both hit their stops
    assert {c["ticker"]: c["reason"] for c in done} == {"AAA": "stop", "BBB": "stop"}
    assert not acct["positions"]


# ── async wrappers (persistence + live pricing) ─────────────────────


@pytest.mark.asyncio
async def test_open_mark_close_roundtrip(tmp_db, monkeypatch):
    monkeypatch.setattr(paper.market_data, "get_quote", lambda tk, m, max_age=None: 110.0)
    s = await paper.open_idea("IN", "AAA", "long", qty=10, stop=95, target=130, price=100.0, source="lab")
    assert s["open_count"] == 1
    pid = s["open_positions"][0]["id"]
    s2 = await paper.mark_account("IN")                       # live 110, target 130 not hit → open
    assert s2["open_count"] == 1 and s2["unrealized_pnl"] == 100.0
    s3 = await paper.close_idea("IN", pid)                    # close at live 110 → +100 realized
    assert s3["open_count"] == 0 and s3["realized_pnl"] == 100.0


@pytest.mark.asyncio
async def test_mark_auto_closes_on_target(tmp_db, monkeypatch):
    monkeypatch.setattr(paper.market_data, "get_quote", lambda tk, m, max_age=None: 130.0)
    await paper.open_idea("IN", "AAA", "long", qty=10, stop=95, target=125, price=100.0, source="lab")
    s = await paper.mark_account("IN")                        # 130 ≥ target 125 → auto-closed at 125
    assert s["open_count"] == 0 and s["closed_count"] == 1 and s["closed"][-1]["reason"] == "target"
    assert s["realized_pnl"] == 250.0


@pytest.mark.asyncio
async def test_reset_account(tmp_db):
    s = await paper.reset_account("IN", 50_000.0)
    assert s["start_balance"] == 50_000.0 and s["open_count"] == 0 and s["equity"] == 50_000.0
