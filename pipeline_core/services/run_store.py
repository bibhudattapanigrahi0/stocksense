"""
Run persistence + user accounts — SQLite via aiosqlite.

Stores pipeline runs (global history), saved screens, portfolios + NAV snapshots,
watchlists, alerts, and paper accounts, plus the `users` and `sessions` tables that
back login. Everything user-owned is scoped by `user_id`; a call that passes
``user_id=None`` (the MCP server, the background refresh loop, and older tests)
resolves to the singleton **owner** account, so those paths keep working unchanged.

Connections are per-operation, not module-global: a shared aiosqlite connection
binds to the event loop that created it and hangs when reused from another loop.
At this write volume that is cheap. On first use of a given database the schema is
created in its final (per-user) form and a one-time migration upgrades any older
single-user database in place (adds `user_id`, rebuilds the tables whose keys
changed, backfills existing rows to the owner).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta as _timedelta
from typing import Optional

import aiosqlite

from pipeline_core.config import DB_PATH, settings
from pipeline_core.models.schemas import PipelineRun, PipelineStatus, SavedScreen
from pipeline_core.portfolio.models import Portfolio, utcnow
from pipeline_core.services import auth

logger = logging.getLogger(__name__)

# The singleton account that owns pre-accounts data and every ``user_id=None`` call
# (MCP tools, the refresh loop). The first human signup claims it (see app.py).
OWNER_ID = "owner"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    display_name TEXT,
    password_hash TEXT,
    is_owner INTEGER NOT NULL DEFAULT 0,
    prefs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS saved_screens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'owner',
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS portfolios (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'owner',
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pf_snap ON portfolio_snapshots(portfolio_id, ts);

CREATE TABLE IF NOT EXISTS watchlist (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'owner',
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'owner',
    market TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
-- Indexes over user_id are created in _migrate(), after the column is guaranteed
-- to exist (an older DB's tables predate it and are patched there first).

CREATE TABLE IF NOT EXISTS paper_accounts (
    user_id TEXT NOT NULL DEFAULT 'owner',
    market TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (user_id, market)
);
"""

# ── connection + one-time migration ────────────────────────────────

_migrated: set[str] = set()
_mig_lock = asyncio.Lock()


@asynccontextmanager
async def _db():
    """Yield a started connection (row_factory set, schema applied, migrated once)."""
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        await _ensure_migrated(conn)
        yield conn


async def _ensure_migrated(conn: aiosqlite.Connection) -> None:
    key = str(DB_PATH)
    if key in _migrated:
        return
    async with _mig_lock:
        if key in _migrated:
            return
        await _migrate(conn)
        _migrated.add(key)


async def _columns(conn, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {r["name"] for r in await cur.fetchall()}


# Final-form DDL per table, used to rebuild the tables whose keys changed.
_REBUILD = {
    "saved_screens": (
        "CREATE TABLE saved_screens (id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'owner', "
        "name TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(user_id, name))",
        "id, name, created_at, payload",
    ),
    "portfolios": (
        "CREATE TABLE portfolios (id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'owner', "
        "name TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(user_id, name))",
        "id, name, created_at, payload",
    ),
    "paper_accounts": (
        "CREATE TABLE paper_accounts (user_id TEXT NOT NULL DEFAULT 'owner', market TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (user_id, market))",
        "market, updated_at, payload",
    ),
}


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Upgrade an older single-user database in place. No-op on a fresh DB.

    - watchlist / alerts: add the `user_id` column (backfilled to the owner).
    - saved_screens / portfolios / paper_accounts: their keys changed
      (name → (user_id, name); market → (user_id, market)), so rebuild + copy.
    Idempotent and guarded to run once per database path.
    """
    # Simple add-column tables.
    for t in ("watchlist", "alerts"):
        cols = await _columns(conn, t)
        if cols and "user_id" not in cols:
            await conn.execute(f"ALTER TABLE {t} ADD COLUMN user_id TEXT")
            await conn.execute(f"UPDATE {t} SET user_id = ? WHERE user_id IS NULL", (OWNER_ID,))
            logger.info("run_store: migrated %s (added user_id)", t)

    # Rebuild tables whose primary/unique key changed.
    for t, (create_sql, cols_csv) in _REBUILD.items():
        existing = await _columns(conn, t)
        if existing and "user_id" not in existing:
            await conn.execute(f"ALTER TABLE {t} RENAME TO {t}__old")
            await conn.execute(create_sql)
            await conn.execute(
                f"INSERT INTO {t} ({cols_csv}, user_id) SELECT {cols_csv}, ? FROM {t}__old", (OWNER_ID,)
            )
            await conn.execute(f"DROP TABLE {t}__old")
            logger.info("run_store: rebuilt %s for per-user keys", t)

    # Now that user_id exists on every table, create its indexes.
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_mkt ON watchlist(user_id, market, ticker)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_fp ON alerts(user_id, fingerprint)")

    await _ensure_owner(conn)
    # Optional: bootstrap the owner's credentials from the environment.
    if settings.owner_email and settings.owner_password:
        async with conn.execute("SELECT password_hash FROM users WHERE id = ?", (OWNER_ID,)) as cur:
            row = await cur.fetchone()
        if row is not None and not row["password_hash"]:
            await conn.execute(
                "UPDATE users SET email = ?, display_name = ?, password_hash = ? WHERE id = ?",
                (auth.normalize_email(settings.owner_email), "Owner",
                 auth.hash_password(settings.owner_password), OWNER_ID),
            )
            logger.info("run_store: owner account bootstrapped from environment")
    await conn.commit()


async def _ensure_owner(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, display_name, password_hash, is_owner, prefs, created_at) "
        "VALUES (?, ?, ?, ?, 1, '{}', ?)",
        (OWNER_ID, None, "Owner", None, utcnow().isoformat()),
    )


async def _resolve_uid(conn: aiosqlite.Connection, user_id: Optional[str]) -> str:
    """A concrete user id for a query: the caller's, or the owner for None."""
    if user_id:
        return user_id
    await _ensure_owner(conn)
    return OWNER_ID


# ── Runs (global history — not user-scoped) ─────────────────────────


async def save_run(run: PipelineRun) -> None:
    async with _db() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO runs (id, created_at, status, payload) VALUES (?, ?, ?, ?)",
            (run.id, run.created_at.isoformat(), run.status.value, run.model_dump_json()),
        )
        await conn.commit()


async def get_run(run_id: str) -> Optional[PipelineRun]:
    async with _db() as conn:
        async with conn.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return PipelineRun.model_validate_json(row["payload"])


async def list_runs(limit: int = 20, status: Optional[PipelineStatus] = None) -> list[PipelineRun]:
    async with _db() as conn:
        if status is not None:
            query = "SELECT payload FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT ?"
            params: tuple = (status.value, limit)
        else:
            query = "SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
    return [PipelineRun.model_validate_json(r["payload"]) for r in rows]


async def delete_run(run_id: str) -> bool:
    async with _db() as conn:
        cursor = await conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        await conn.commit()
        return cursor.rowcount > 0


# ── Saved screens (per-user) ────────────────────────────────────────


async def save_screen(screen: SavedScreen, user_id: Optional[str] = None) -> SavedScreen:
    if screen.id is None:
        screen.id = str(uuid.uuid4())
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        await conn.execute(
            "INSERT OR REPLACE INTO saved_screens (id, user_id, name, created_at, payload) VALUES (?, ?, ?, ?, ?)",
            (screen.id, uid, screen.name, screen.created_at.isoformat(), screen.model_dump_json()),
        )
        await conn.commit()
    return screen


async def get_screen(screen_id_or_name: str, user_id: Optional[str] = None) -> Optional[SavedScreen]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        async with conn.execute(
            "SELECT payload FROM saved_screens WHERE user_id = ? AND (id = ? OR name = ?)",
            (uid, screen_id_or_name, screen_id_or_name),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return SavedScreen.model_validate_json(row["payload"])


async def list_screens(user_id: Optional[str] = None) -> list[SavedScreen]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        async with conn.execute(
            "SELECT payload FROM saved_screens WHERE user_id = ? ORDER BY created_at DESC", (uid,)
        ) as cur:
            rows = await cur.fetchall()
    return [SavedScreen.model_validate_json(r["payload"]) for r in rows]


async def delete_screen(screen_id_or_name: str, user_id: Optional[str] = None) -> bool:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        cursor = await conn.execute(
            "DELETE FROM saved_screens WHERE user_id = ? AND (id = ? OR name = ?)",
            (uid, screen_id_or_name, screen_id_or_name),
        )
        await conn.commit()
        return cursor.rowcount > 0


# ── Portfolios (per-user) ───────────────────────────────────────────


async def save_portfolio(portfolio: Portfolio, user_id: Optional[str] = None) -> Portfolio:
    """Insert or update a portfolio (keyed by id; name is unique per user)."""
    if portfolio.id is None:
        portfolio.id = str(uuid.uuid4())
    portfolio.updated_at = utcnow()
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        await conn.execute(
            "INSERT OR REPLACE INTO portfolios (id, user_id, name, created_at, payload) VALUES (?, ?, ?, ?, ?)",
            (portfolio.id, uid, portfolio.name, portfolio.created_at.isoformat(), portfolio.model_dump_json()),
        )
        await conn.commit()
    return portfolio


async def get_portfolio(portfolio_id_or_name: str, user_id: Optional[str] = None) -> Optional[Portfolio]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        async with conn.execute(
            "SELECT payload FROM portfolios WHERE user_id = ? AND (id = ? OR name = ?)",
            (uid, portfolio_id_or_name, portfolio_id_or_name),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return Portfolio.model_validate_json(row["payload"])


async def list_portfolios(user_id: Optional[str] = None) -> list[Portfolio]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        async with conn.execute(
            "SELECT payload FROM portfolios WHERE user_id = ? ORDER BY created_at DESC", (uid,)
        ) as cur:
            rows = await cur.fetchall()
    return [Portfolio.model_validate_json(r["payload"]) for r in rows]


async def delete_portfolio(portfolio_id_or_name: str, user_id: Optional[str] = None) -> bool:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        # Resolve the owned portfolio's id first so the snapshot cascade is exact.
        async with conn.execute(
            "SELECT id FROM portfolios WHERE user_id = ? AND (id = ? OR name = ?)",
            (uid, portfolio_id_or_name, portfolio_id_or_name),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        pid = row["id"]
        await conn.execute("DELETE FROM portfolios WHERE id = ? AND user_id = ?", (pid, uid))
        await conn.execute("DELETE FROM portfolio_snapshots WHERE portfolio_id = ?", (pid,))
        await conn.commit()
        return True


# ── Portfolio snapshots (NAV history; ownership enforced via the portfolio) ──


async def save_portfolio_snapshot(portfolio_id: str, snapshot: dict) -> dict:
    snap_id = str(uuid.uuid4())
    ts = snapshot.get("ts") or utcnow().isoformat()
    async with _db() as conn:
        await conn.execute(
            "INSERT INTO portfolio_snapshots (id, portfolio_id, ts, payload) VALUES (?, ?, ?, ?)",
            (snap_id, portfolio_id, ts, json.dumps(snapshot)),
        )
        await conn.commit()
    return snapshot


async def list_portfolio_snapshots(portfolio_id: str, limit: int = 500) -> list[dict]:
    async with _db() as conn:
        async with conn.execute(
            "SELECT payload FROM portfolio_snapshots WHERE portfolio_id = ? ORDER BY ts ASC LIMIT ?",
            (portfolio_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [json.loads(r["payload"]) for r in rows]


async def latest_portfolio_snapshot(portfolio_id: str) -> Optional[dict]:
    async with _db() as conn:
        async with conn.execute(
            "SELECT payload FROM portfolio_snapshots WHERE portfolio_id = ? ORDER BY ts DESC LIMIT 1",
            (portfolio_id,),
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row["payload"]) if row else None


# ── Watchlist (per-user) ────────────────────────────────────────────


async def add_watch(item: dict, user_id: Optional[str] = None) -> dict:
    item = dict(item)
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("created_at", utcnow().isoformat())
    item["market"] = (item.get("market") or "IN").upper()
    item["ticker"] = item["ticker"].upper()
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        await conn.execute(
            "INSERT OR REPLACE INTO watchlist (id, user_id, market, ticker, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item["id"], uid, item["market"], item["ticker"], item["created_at"], json.dumps(item)),
        )
        await conn.commit()
    return item


async def list_watch(market: Optional[str] = None, user_id: Optional[str] = None) -> list[dict]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        if market:
            q = "SELECT payload FROM watchlist WHERE user_id = ? AND market = ? ORDER BY created_at DESC"
            p: tuple = (uid, market.upper())
        else:
            q, p = ("SELECT payload FROM watchlist WHERE user_id = ? ORDER BY created_at DESC", (uid,))
        async with conn.execute(q, p) as cur:
            rows = await cur.fetchall()
    return [json.loads(r["payload"]) for r in rows]


async def delete_watch(watch_id: str, user_id: Optional[str] = None) -> bool:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        cur = await conn.execute("DELETE FROM watchlist WHERE id = ? AND user_id = ?", (watch_id, uid))
        await conn.commit()
        return cur.rowcount > 0


# ── Alerts (per-user) ───────────────────────────────────────────────


async def save_alert(alert: dict, *, dedup_hours: int = 24, user_id: Optional[str] = None) -> Optional[dict]:
    """Persist an alert unless one with the same `fingerprint` exists for this user within
    the last `dedup_hours`. Returns the stored alert, or None if suppressed as a duplicate."""
    alert = dict(alert)
    alert.setdefault("id", str(uuid.uuid4()))
    alert.setdefault("created_at", utcnow().isoformat())
    alert["market"] = (alert.get("market") or "IN").upper()
    alert.setdefault("read", False)
    fp = alert.get("fingerprint")
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        if fp:
            cutoff = (utcnow() - _timedelta(hours=dedup_hours)).isoformat()
            async with conn.execute(
                "SELECT 1 FROM alerts WHERE user_id = ? AND fingerprint = ? AND created_at >= ? LIMIT 1",
                (uid, fp, cutoff),
            ) as cur:
                if await cur.fetchone():
                    return None
        await conn.execute(
            "INSERT INTO alerts (id, user_id, market, created_at, read, fingerprint, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alert["id"], uid, alert["market"], alert["created_at"], int(bool(alert["read"])), fp, json.dumps(alert)),
        )
        await conn.commit()
    return alert


async def list_alerts(market: Optional[str] = None, *, unread_only: bool = False,
                      limit: int = 50, user_id: Optional[str] = None) -> list[dict]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        clauses, params = ["user_id = ?"], [uid]
        if market:
            clauses.append("market = ?"); params.append(market.upper())
        if unread_only:
            clauses.append("read = 0")
        where = " WHERE " + " AND ".join(clauses)
        async with conn.execute(
            f"SELECT payload, read FROM alerts{where} ORDER BY created_at DESC LIMIT ?", (*params, limit)
        ) as cur:
            rows = await cur.fetchall()
    out = []
    for r in rows:
        a = json.loads(r["payload"])
        a["read"] = bool(r["read"])
        out.append(a)
    return out


async def unread_count(market: Optional[str] = None, user_id: Optional[str] = None) -> int:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        q = "SELECT COUNT(*) AS n FROM alerts WHERE user_id = ? AND read = 0" + (" AND market = ?" if market else "")
        params = (uid, market.upper()) if market else (uid,)
        async with conn.execute(q, params) as cur:
            row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def mark_alerts_read(ids: Optional[list[str]] = None, market: Optional[str] = None,
                           user_id: Optional[str] = None) -> int:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        if ids:
            qs = ",".join("?" * len(ids))
            cur = await conn.execute(
                f"UPDATE alerts SET read = 1 WHERE user_id = ? AND id IN ({qs})", (uid, *ids)
            )
        elif market:
            cur = await conn.execute(
                "UPDATE alerts SET read = 1 WHERE user_id = ? AND market = ?", (uid, market.upper())
            )
        else:
            cur = await conn.execute("UPDATE alerts SET read = 1 WHERE user_id = ?", (uid,))
        await conn.commit()
        return cur.rowcount


async def clear_alerts(market: Optional[str] = None, user_id: Optional[str] = None) -> int:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        if market:
            cur = await conn.execute("DELETE FROM alerts WHERE user_id = ? AND market = ?", (uid, market.upper()))
        else:
            cur = await conn.execute("DELETE FROM alerts WHERE user_id = ?", (uid,))
        await conn.commit()
        return cur.rowcount


# ── Paper-trading accounts (one per user × market) ──────────────────


async def get_paper(market: str, user_id: Optional[str] = None) -> Optional[dict]:
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        async with conn.execute(
            "SELECT payload FROM paper_accounts WHERE user_id = ? AND market = ?", (uid, market.upper())
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row["payload"]) if row else None


async def save_paper(account: dict, user_id: Optional[str] = None) -> dict:
    mk = (account.get("market") or "IN").upper()
    account["market"] = mk
    async with _db() as conn:
        uid = await _resolve_uid(conn, user_id)
        await conn.execute(
            "INSERT OR REPLACE INTO paper_accounts (user_id, market, updated_at, payload) VALUES (?, ?, ?, ?)",
            (uid, mk, account.get("updated_at") or utcnow().isoformat(), json.dumps(account)),
        )
        await conn.commit()
    return account


# ── Users & sessions (authentication) ───────────────────────────────


def _user_dict(row) -> dict:
    """Public user shape (never includes the password hash)."""
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"] or "",
        "is_owner": bool(row["is_owner"]),
        "prefs": json.loads(row["prefs"] or "{}"),
        "created_at": row["created_at"],
        "has_password": bool(row["password_hash"]),
    }


async def get_user_by_id(user_id: str) -> Optional[dict]:
    async with _db() as conn:
        async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return _user_dict(row) if row else None


async def get_user_by_email(email: str) -> Optional[dict]:
    async with _db() as conn:
        async with conn.execute("SELECT * FROM users WHERE email = ?", (auth.normalize_email(email),)) as cur:
            row = await cur.fetchone()
    return _user_dict(row) if row else None


async def get_owner() -> dict:
    """The owner account (created unclaimed if it does not exist yet)."""
    async with _db() as conn:
        await _ensure_owner(conn)
        await conn.commit()
        async with conn.execute("SELECT * FROM users WHERE id = ?", (OWNER_ID,)) as cur:
            row = await cur.fetchone()
    return _user_dict(row)


async def count_users(claimed_only: bool = False) -> int:
    q = "SELECT COUNT(*) AS n FROM users" + (" WHERE password_hash IS NOT NULL AND password_hash != ''" if claimed_only else "")
    async with _db() as conn:
        async with conn.execute(q) as cur:
            row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def create_user(email: str, password_hash: str, display_name: str = "") -> dict:
    """Create a normal (non-owner) account. Raises ValueError if the email is taken."""
    email = auth.normalize_email(email)
    uid = str(uuid.uuid4())
    async with _db() as conn:
        async with conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)) as cur:
            if await cur.fetchone():
                raise ValueError("email already registered")
        await conn.execute(
            "INSERT INTO users (id, email, display_name, password_hash, is_owner, prefs, created_at) "
            "VALUES (?, ?, ?, ?, 0, '{}', ?)",
            (uid, email, display_name or email.split("@")[0], password_hash, utcnow().isoformat()),
        )
        await conn.commit()
        async with conn.execute("SELECT * FROM users WHERE id = ?", (uid,)) as cur:
            row = await cur.fetchone()
    return _user_dict(row)


async def claim_owner(email: str, password_hash: str, display_name: str = "") -> dict:
    """The first signup adopts the owner account (which holds all pre-accounts data)."""
    email = auth.normalize_email(email)
    async with _db() as conn:
        await _ensure_owner(conn)
        async with conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (email, OWNER_ID)) as cur:
            if await cur.fetchone():
                raise ValueError("email already registered")
        await conn.execute(
            "UPDATE users SET email = ?, display_name = ?, password_hash = ? WHERE id = ?",
            (email, display_name or email.split("@")[0], password_hash, OWNER_ID),
        )
        await conn.commit()
        async with conn.execute("SELECT * FROM users WHERE id = ?", (OWNER_ID,)) as cur:
            row = await cur.fetchone()
    return _user_dict(row)


async def auth_login(email: str, password: str) -> Optional[dict]:
    """Return the user on correct credentials, else None (timing-safe verify)."""
    async with _db() as conn:
        async with conn.execute("SELECT * FROM users WHERE email = ?", (auth.normalize_email(email),)) as cur:
            row = await cur.fetchone()
    if row is None or not auth.verify_password(password, row["password_hash"]):
        return None
    return _user_dict(row)


async def update_user_prefs(user_id: str, prefs: dict) -> dict:
    async with _db() as conn:
        await conn.execute("UPDATE users SET prefs = ? WHERE id = ?", (json.dumps(prefs), user_id))
        await conn.commit()
        async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return _user_dict(row) if row else {}


async def update_user_profile(user_id: str, *, display_name: Optional[str] = None,
                              password_hash: Optional[str] = None) -> dict:
    sets, params = [], []
    if display_name is not None:
        sets.append("display_name = ?"); params.append(display_name)
    if password_hash is not None:
        sets.append("password_hash = ?"); params.append(password_hash)
    async with _db() as conn:
        if sets:
            params.append(user_id)
            await conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params))
            await conn.commit()
        async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return _user_dict(row) if row else {}


async def create_session(user_id: str, ttl_days: int = 30) -> str:
    token = auth.new_token()
    now = utcnow()
    expires = now + _timedelta(days=ttl_days)
    async with _db() as conn:
        await conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires.isoformat()),
        )
        await conn.commit()
    return token


async def get_session_user(token: str) -> Optional[dict]:
    """Resolve a session token to its user, or None if missing/expired."""
    if not token:
        return None
    async with _db() as conn:
        async with conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, utcnow().isoformat()),
        ) as cur:
            row = await cur.fetchone()
    return _user_dict(row) if row else None


async def delete_session(token: str) -> None:
    async with _db() as conn:
        await conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await conn.commit()


async def delete_user_sessions(user_id: str) -> None:
    async with _db() as conn:
        await conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await conn.commit()
