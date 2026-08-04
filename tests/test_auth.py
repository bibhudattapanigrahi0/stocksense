"""
Authentication + per-user data isolation + single-user → multi-user migration.

Network-free: exercises the stdlib scrypt hashing, the users/sessions tables, and
the per-user scoping of portfolios / watchlist / paper / alerts against a tmp DB.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from pipeline_core.portfolio.models import Portfolio, PortfolioSpec
from pipeline_core.services import auth
from pipeline_core.services import run_store


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "DB_PATH", tmp_path / "auth.db")
    run_store._migrated.discard(str(tmp_path / "auth.db"))
    return tmp_path


def _pf(name: str) -> Portfolio:
    return Portfolio(name=name, spec=PortfolioSpec(name=name))


# ── password hashing (pure, no DB) ──────────────────────────────────

def test_hash_verify_roundtrip():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("scrypt$") and h != auth.hash_password("correct horse battery staple")  # random salt
    assert auth.verify_password("correct horse battery staple", h)
    assert not auth.verify_password("wrong", h)


def test_verify_rejects_empty_and_malformed():
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "")
    assert not auth.verify_password("x", "not-a-scrypt-hash")
    assert not auth.verify_password("", auth.hash_password("secret12"))


def test_hash_empty_password_rejected():
    with pytest.raises(ValueError):
        auth.hash_password("")


def test_tokens_unique_and_email_normalized():
    assert auth.new_token() != auth.new_token()
    assert auth.normalize_email("  Foo@Bar.COM ") == "foo@bar.com"


# ── users + sessions ────────────────────────────────────────────────

async def test_owner_created_unclaimed(tmp_db):
    owner = await run_store.get_owner()
    assert owner["id"] == "owner" and owner["is_owner"] and not owner["has_password"]


async def test_create_user_and_login(tmp_db):
    u = await run_store.create_user("alice@x.com", auth.hash_password("password1"), "Alice")
    assert u["email"] == "alice@x.com" and not u["is_owner"]
    assert (await run_store.auth_login("alice@x.com", "password1"))["id"] == u["id"]
    assert await run_store.auth_login("alice@x.com", "nope") is None
    assert await run_store.auth_login("ghost@x.com", "password1") is None


async def test_duplicate_email_rejected(tmp_db):
    await run_store.create_user("a@x.com", auth.hash_password("password1"))
    with pytest.raises(ValueError):
        await run_store.create_user("A@X.com", auth.hash_password("password2"))  # case-insensitive


async def test_sessions_lifecycle(tmp_db):
    u = await run_store.create_user("s@x.com", auth.hash_password("password1"))
    tok = await run_store.create_session(u["id"], ttl_days=1)
    assert (await run_store.get_session_user(tok))["id"] == u["id"]
    await run_store.delete_session(tok)
    assert await run_store.get_session_user(tok) is None


async def test_expired_session_is_rejected(tmp_db):
    u = await run_store.create_user("e@x.com", auth.hash_password("password1"))
    tok = await run_store.create_session(u["id"], ttl_days=-1)   # already expired
    assert await run_store.get_session_user(tok) is None
    assert await run_store.get_session_user("garbage-token") is None


async def test_owner_claim_and_prefs(tmp_db):
    owner = await run_store.claim_owner("me@x.com", auth.hash_password("password1"), "Me")
    assert owner["id"] == "owner" and owner["email"] == "me@x.com" and owner["has_password"]
    assert (await run_store.auth_login("me@x.com", "password1"))["id"] == "owner"
    updated = await run_store.update_user_prefs("owner", {"theme": "dark", "accent": "teal"})
    assert updated["prefs"] == {"theme": "dark", "accent": "teal"}


# ── per-user data isolation ─────────────────────────────────────────

async def test_portfolio_isolation(tmp_db):
    a = await run_store.create_user("a@x.com", auth.hash_password("password1"))
    b = await run_store.create_user("b@x.com", auth.hash_password("password1"))
    await run_store.save_portfolio(_pf("Core"), user_id=a["id"])
    await run_store.save_portfolio(_pf("Core"), user_id=b["id"])   # same name, different user → allowed
    assert len(await run_store.list_portfolios(user_id=a["id"])) == 1
    assert len(await run_store.list_portfolios(user_id=b["id"])) == 1
    assert await run_store.get_portfolio("Core", user_id=a["id"]) is not None
    # A deleting "Core" must not touch B's identically-named portfolio.
    assert await run_store.delete_portfolio("Core", user_id=a["id"]) is True
    assert len(await run_store.list_portfolios(user_id=a["id"])) == 0
    assert len(await run_store.list_portfolios(user_id=b["id"])) == 1


async def test_watchlist_and_paper_isolation(tmp_db):
    a = await run_store.create_user("a@x.com", auth.hash_password("password1"))
    b = await run_store.create_user("b@x.com", auth.hash_password("password1"))
    await run_store.add_watch({"ticker": "RELIANCE", "market": "IN"}, user_id=a["id"])
    assert len(await run_store.list_watch(user_id=a["id"])) == 1
    assert len(await run_store.list_watch(user_id=b["id"])) == 0
    # Same market, one account per user.
    await run_store.save_paper({"market": "IN", "cash": 100}, user_id=a["id"])
    await run_store.save_paper({"market": "IN", "cash": 200}, user_id=b["id"])
    assert (await run_store.get_paper("IN", user_id=a["id"]))["cash"] == 100
    assert (await run_store.get_paper("IN", user_id=b["id"]))["cash"] == 200


async def test_alert_dedup_is_per_user(tmp_db):
    a = await run_store.create_user("a@x.com", auth.hash_password("password1"))
    b = await run_store.create_user("b@x.com", auth.hash_password("password1"))
    fp = {"market": "IN", "fingerprint": "fp1", "title": "t", "type": "x"}
    assert await run_store.save_alert(dict(fp), user_id=a["id"]) is not None
    assert await run_store.save_alert(dict(fp), user_id=a["id"]) is None       # deduped for A
    assert await run_store.save_alert(dict(fp), user_id=b["id"]) is not None   # B is independent
    assert await run_store.unread_count(user_id=a["id"]) == 1
    assert await run_store.unread_count(user_id=b["id"]) == 1


async def test_none_user_id_resolves_to_owner(tmp_db):
    # The MCP server + background loop pass no user_id and must land on the owner.
    await run_store.save_paper({"market": "US", "cash": 42}, user_id=None)
    assert (await run_store.get_paper("US"))["cash"] == 42
    assert (await run_store.get_paper("US", user_id="owner"))["cash"] == 42


# ── single-user → multi-user migration ──────────────────────────────

async def test_migration_backfills_legacy_data_to_owner(tmp_db):
    path = run_store.DB_PATH
    pf = _pf("Legacy")
    async with aiosqlite.connect(str(path)) as c:
        await c.executescript(
            "CREATE TABLE portfolios (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);"
            "CREATE TABLE saved_screens (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);"
            "CREATE TABLE watchlist (id TEXT PRIMARY KEY, market TEXT NOT NULL, ticker TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);"
            "CREATE TABLE alerts (id TEXT PRIMARY KEY, market TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0, fingerprint TEXT, payload TEXT NOT NULL);"
            "CREATE TABLE paper_accounts (market TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL);"
        )
        await c.execute("INSERT INTO portfolios VALUES (?,?,?,?)",
                        (pf.id or "pid", "Legacy", pf.created_at.isoformat(), pf.model_dump_json()))
        await c.execute("INSERT INTO watchlist VALUES (?,?,?,?,?)",
                        ("w1", "IN", "TCS", "2025-01-01T00:00:00", json.dumps({"id": "w1", "ticker": "TCS", "market": "IN"})))
        await c.execute("INSERT INTO paper_accounts VALUES (?,?,?)",
                        ("IN", "2025-01-01T00:00:00", json.dumps({"market": "IN", "cash": 500})))
        await c.commit()
    run_store._migrated.discard(str(path))

    # First access migrates; the owner inherits all the legacy rows.
    pfs = await run_store.list_portfolios()
    assert [p.name for p in pfs] == ["Legacy"]
    assert (await run_store.list_watch())[0]["ticker"] == "TCS"
    assert (await run_store.get_paper("IN"))["cash"] == 500

    # paper_accounts is rebuilt to (user_id, market): a second user can hold IN too.
    await run_store.save_paper({"market": "IN", "cash": 11}, user_id="someuser")
    assert (await run_store.get_paper("IN"))["cash"] == 500                      # owner untouched
    assert (await run_store.get_paper("IN", user_id="someuser"))["cash"] == 11
