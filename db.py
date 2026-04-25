"""
PostgreSQL persistence: multi-tenant (user_id) CrowdWorks Bot data.

* users           — app login (web); id=1 is the desktop "local" user
* user_settings   — per-user key / value
* prompts, accounts, bids, logs — all scoped with user_id

Desktop code uses ``DESKTOP_USER_ID`` (1) for all calls. Web uses the signed-in user.

Environment variable ``DATABASE_URL`` overrides the built-in default connection string.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://cwb_qwrt_user:4revyaquJWdKwAg80daxM8RYSrLUJnMc"
    "@dpg-d7madhqqqhas73f8m8ag-a.virginia-postgres.render.com/cwb_qwrt",
)

# Single-user / legacy desktop: always this row in ``users`` (email __local@desktop).
DESKTOP_USER_ID = 1

# Built-in administrator account created on first run.
ADMIN_EMAIL = "admin@admin.admin"
_ADMIN_PASSWORD = "Cobra_1983730"

# User roles:  'admin' | 'active' | 'pending' | 'rejected'
# New registrations start as 'pending' and require admin approval.

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


class _Conn:
    """Wraps a psycopg2 connection + RealDictCursor to mimic the sqlite3 API."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._cur: psycopg2.extras.RealDictCursor = raw.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        )

    def execute(self, sql: str, params: Any = None) -> psycopg2.extras.RealDictCursor:
        self._cur.execute(sql, params)
        return self._cur


def _row_to_dict(row: Any) -> dict:
    """Convert a RealDictRow to a plain dict, normalising datetime → ISO string."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.strftime("%Y-%m-%d %H:%M:%S")
    return d


@contextmanager
def _conn() -> Generator[_Conn, None, None]:
    pool = _get_pool()
    raw = pool.getconn()
    try:
        raw.autocommit = False
        con = _Conn(raw)
        yield con
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        pool.putconn(raw)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS prompts (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS accounts (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER,
    name          TEXT NOT NULL DEFAULT 'Account',
    session_id    TEXT NOT NULL DEFAULT '',
    cw_username   TEXT NOT NULL DEFAULT '',
    prompt_id     INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'unverified',
    last_verified TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bids (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER,
    account_id   INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    job_id       TEXT NOT NULL,
    job_title    TEXT NOT NULL DEFAULT '',
    result_url   TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'success',
    error_msg    TEXT NOT NULL DEFAULT '',
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bid_scoped   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, bid_scoped, job_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def init() -> None:
    with _conn() as con:
        for stmt in _SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)

    _migrate_schema()
    _ensure_local_desktop_user()
    _ensure_admin_user()
    _migrate_from_json()


def _migrate_schema() -> None:
    """Add columns introduced after the initial schema so existing DBs stay compatible."""
    with _conn() as con:
        has_role = con.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'users' AND column_name = 'role'",
        ).fetchone()
        if not has_role:
            con.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'pending'")
            # Existing desktop / admin rows become admin; everyone else was already active.
            con.execute(
                "UPDATE users SET role = 'admin'"
                " WHERE id = %s OR lower(email) = %s",
                (DESKTOP_USER_ID, ADMIN_EMAIL),
            )
            con.execute(
                "UPDATE users SET role = 'active'"
                " WHERE role = 'pending' AND id != %s AND lower(email) != %s",
                (DESKTOP_USER_ID, ADMIN_EMAIL),
            )


def _ensure_local_desktop_user() -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO users (id, email, password_hash, role)"
            " VALUES (1, '__local@desktop', '*', 'admin')"
            " ON CONFLICT DO NOTHING"
        )
        # Keep the sequence ahead of the manually-inserted id=1 row.
        con.execute(
            "SELECT setval("
            "  pg_get_serial_sequence('users', 'id'),"
            "  GREATEST(1, (SELECT MAX(id) FROM users))"
            ")"
        )


def _ensure_admin_user() -> None:
    """Create (or repair) the built-in admin account."""
    import bcrypt as _bcrypt

    existing = user_by_email(ADMIN_EMAIL)
    if existing:
        if existing.get("role") != "admin":
            set_user_role(int(existing["id"]), "admin")
        return
    h = _bcrypt.hashpw(_ADMIN_PASSWORD.encode(), _bcrypt.gensalt(rounds=12)).decode("ascii")
    with _conn() as con:
        con.execute(
            "INSERT INTO users (email, password_hash, role)"
            " VALUES (%s, %s, 'admin')"
            " ON CONFLICT (email) DO UPDATE SET role = 'admin'",
            (ADMIN_EMAIL, h),
        )


def _migrate_from_json() -> None:
    """One-time migration: import settings from the old local_settings.json file."""
    import json as _json

    from _appdir import APP_DIR

    path = APP_DIR / "local_settings.json"
    if not path.exists():
        return
    try:
        data: dict = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM user_settings WHERE user_id = %s LIMIT 1",
            (DESKTOP_USER_ID,),
        ).fetchone()
        if row:
            return

    old_key = str(data.get("openai_api_key") or "").strip()
    if old_key:
        set_setting(DESKTOP_USER_ID, "openai_api_key", old_key)

    old_session = str(data.get("cw_session_id") or "").strip()
    old_prompt = str(data.get("proposal_extra_prompt") or "").strip()
    pid: int | None = None

    with _conn() as con:
        pcount = con.execute(
            "SELECT COUNT(*) AS n FROM prompts WHERE user_id = %s", (DESKTOP_USER_ID,)
        ).fetchone()
        pcounti = int(pcount["n"]) if pcount else 0

    if old_prompt and pcounti == 0:
        pid = add_prompt(DESKTOP_USER_ID, "Default Prompt", old_prompt)

    with _conn() as con:
        acn = con.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE user_id = %s", (DESKTOP_USER_ID,)
        ).fetchone()
        nacc = int(acn["n"]) if acn else 0

    if old_session and nacc == 0:
        if pid is None:
            pp = list_prompts(DESKTOP_USER_ID)
            pid = int(pp[0]["id"]) if pp else None
        add_account(DESKTOP_USER_ID, "Account 1", old_session, prompt_id=pid)

    if not get_setting(DESKTOP_USER_ID, "_migrated"):
        set_setting(DESKTOP_USER_ID, "_migrated", "1")


def _bid_scoped_id(account_id: int | None) -> int:
    return int(account_id) if account_id is not None else 0


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def user_register(email: str, password_hash: str) -> int:
    """Register a new user with role='pending' (requires admin approval)."""
    with _conn() as con:
        row = con.execute(
            "INSERT INTO users(email, password_hash, role) VALUES(%s, %s, 'pending') RETURNING id",
            (email, password_hash),
        ).fetchone()
    return int(row["id"]) if row else 0


def list_all_users() -> list[dict]:
    """Return all non-desktop users ordered by pending first, for the admin panel."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT id, email, role, created_at FROM users
            WHERE id != %s
            ORDER BY
                CASE role
                    WHEN 'pending'  THEN 0
                    WHEN 'active'   THEN 1
                    WHEN 'admin'    THEN 2
                    ELSE 3
                END,
                id
            """,
            (DESKTOP_USER_ID,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_user_role(user_id: int, role: str) -> None:
    """Update a user's role. The desktop system account cannot be changed."""
    with _conn() as con:
        con.execute(
            "UPDATE users SET role = %s WHERE id = %s AND id != %s",
            (role, user_id, DESKTOP_USER_ID),
        )


def user_by_email(email: str) -> dict | None:
    e = (email or "").strip().lower()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE lower(email) = %s", (e,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def user_by_id(uid: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE id = %s", (uid,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def is_internal_desktop_user(uid: int) -> bool:
    if uid == DESKTOP_USER_ID:
        return True
    u = user_by_id(uid)
    return u is not None and (u.get("email") or "").lower() == "__local@desktop"


# ---------------------------------------------------------------------------
# Settings (per user)
# ---------------------------------------------------------------------------

def get_setting(user_id: int, key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM user_settings WHERE user_id = %s AND key = %s",
            (user_id, key),
        ).fetchone()
    return str(row["value"]) if row else default


def set_setting(user_id: int, key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO user_settings(user_id, key, value) VALUES(%s, %s, %s)"
            " ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
            (user_id, key, value),
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def list_prompts(user_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM prompts WHERE user_id = %s ORDER BY id", (user_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_prompt(user_id: int, prompt_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM prompts WHERE id = %s AND user_id = %s",
            (prompt_id, user_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def add_prompt(user_id: int, name: str, content: str) -> int:
    with _conn() as con:
        row = con.execute(
            "INSERT INTO prompts(user_id, name, content) VALUES(%s, %s, %s) RETURNING id",
            (user_id, name, content),
        ).fetchone()
    return int(row["id"]) if row else 0


def update_prompt(user_id: int, prompt_id: int, name: str, content: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE prompts SET name = %s, content = %s WHERE id = %s AND user_id = %s",
            (name, content, prompt_id, user_id),
        )


def delete_prompt(user_id: int, prompt_id: int) -> None:
    with _conn() as con:
        con.execute(
            "DELETE FROM prompts WHERE id = %s AND user_id = %s",
            (prompt_id, user_id),
        )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def list_accounts(user_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT a.id, a.name, a.session_id, a.cw_username, a.user_id,
                   a.prompt_id, a.enabled, a.status, a.last_verified, a.created_at,
                   p.name AS prompt_name, p.content AS prompt_content
            FROM accounts a
            LEFT JOIN prompts p ON a.prompt_id = p.id
            WHERE a.user_id = %s
            ORDER BY a.id
            """,
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_account(user_id: int, account_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """
            SELECT a.*, p.name AS prompt_name, p.content AS prompt_content
            FROM accounts a
            LEFT JOIN prompts p ON a.prompt_id = p.id
            WHERE a.id = %s AND a.user_id = %s
            """,
            (account_id, user_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def add_account(
    user_id: int,
    name: str,
    session_id: str,
    *,
    prompt_id: int | None = None,
    enabled: int = 1,
) -> int:
    with _conn() as con:
        row = con.execute(
            "INSERT INTO accounts(user_id, name, session_id, prompt_id, enabled)"
            " VALUES(%s, %s, %s, %s, %s) RETURNING id",
            (user_id, name, session_id, prompt_id, enabled),
        ).fetchone()
    return int(row["id"]) if row else 0


def update_account(user_id: int, account_id: int, **kwargs: Any) -> None:
    allowed = {
        "name", "session_id", "cw_username", "prompt_id", "enabled", "status", "last_verified"
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    vals = list(fields.values()) + [account_id, user_id]
    with _conn() as con:
        con.execute(
            f"UPDATE accounts SET {cols} WHERE id = %s AND user_id = %s", vals
        )


def delete_account(user_id: int, account_id: int) -> None:
    with _conn() as con:
        con.execute(
            "DELETE FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, user_id),
        )


# ---------------------------------------------------------------------------
# Bids
# ---------------------------------------------------------------------------

def has_bid(user_id: int, account_id: int | None, job_id: str) -> bool:
    bsc = _bid_scoped_id(account_id)
    with _conn() as con:
        row = con.execute(
            """
            SELECT 1 FROM bids
            WHERE user_id = %s AND job_id = %s AND bid_scoped = %s
              AND status IN ('success', 'already_bid')
            """,
            (user_id, str(job_id), bsc),
        ).fetchone()
    return row is not None


def record_bid(
    user_id: int,
    job_id: str,
    *,
    job_title: str = "",
    result_url: str = "",
    account_id: int | None = None,
    status: str = "success",
    error_msg: str = "",
) -> None:
    bsc = _bid_scoped_id(account_id)
    job_id = str(job_id)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO bids(
                user_id, account_id, bid_scoped, job_id, job_title, result_url, status, error_msg
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, bid_scoped, job_id) DO UPDATE SET
              status = CASE
                WHEN CASE EXCLUDED.status
                       WHEN 'success'     THEN 2
                       WHEN 'already_bid' THEN 1
                       ELSE 0 END
                   > CASE bids.status
                       WHEN 'success'     THEN 2
                       WHEN 'already_bid' THEN 1
                       ELSE 0 END
                  THEN EXCLUDED.status
                  ELSE bids.status
                END,
              result_url = CASE
                WHEN EXCLUDED.status = 'success'
                  THEN EXCLUDED.result_url
                  ELSE bids.result_url
                END,
              error_msg = CASE
                WHEN EXCLUDED.status IN ('success', 'already_bid')
                  THEN ''
                  ELSE EXCLUDED.error_msg
                END,
              submitted_at = CASE
                WHEN EXCLUDED.status IN ('success', 'already_bid')
                  THEN NOW()
                  ELSE bids.submitted_at
                END,
              job_title = EXCLUDED.job_title
            """,
            (user_id, account_id, bsc, job_id, job_title, result_url, status, error_msg),
        )


def delete_bid(user_id: int, account_id: int | None, job_id: str) -> None:
    bsc = _bid_scoped_id(account_id)
    with _conn() as con:
        con.execute(
            "DELETE FROM bids WHERE user_id = %s AND job_id = %s AND bid_scoped = %s",
            (user_id, str(job_id), bsc),
        )


def get_bid_job_ids(user_id: int, account_id: int | None = None) -> set[str]:
    with _conn() as con:
        if account_id is not None:
            bsc = _bid_scoped_id(account_id)
            rows = con.execute(
                "SELECT job_id FROM bids WHERE user_id = %s AND bid_scoped = %s",
                (user_id, bsc),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT job_id FROM bids WHERE user_id = %s", (user_id,)
            ).fetchall()
    return {r["job_id"] for r in rows}


def get_bid_statuses(user_id: int, account_id: int | None) -> dict[str, str]:
    with _conn() as con:
        if account_id is not None:
            bsc = _bid_scoped_id(account_id)
            rows = con.execute(
                "SELECT job_id, status FROM bids WHERE user_id = %s AND bid_scoped = %s",
                (user_id, bsc),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT job_id,
                  CASE
                    WHEN MAX(CASE status WHEN 'success'     THEN 2 ELSE 0 END) = 2
                         THEN 'success'
                    WHEN MAX(CASE status WHEN 'already_bid' THEN 1 ELSE 0 END) = 1
                         THEN 'already_bid'
                    ELSE 'failed'
                  END AS status
                FROM bids
                WHERE user_id = %s
                GROUP BY job_id
                """,
                (user_id,),
            ).fetchall()
    return {r["job_id"]: r["status"] for r in rows}


def get_bid_errors(user_id: int, account_id: int | None) -> dict[str, str]:
    with _conn() as con:
        if account_id is not None:
            bsc = _bid_scoped_id(account_id)
            rows = con.execute(
                "SELECT job_id, error_msg FROM bids"
                " WHERE user_id = %s AND bid_scoped = %s AND status = 'failed'"
                " ORDER BY submitted_at DESC",
                (user_id, bsc),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT job_id, error_msg FROM bids"
                " WHERE user_id = %s AND status = 'failed'"
                " ORDER BY submitted_at DESC",
                (user_id,),
            ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if r["job_id"] not in out:
            out[r["job_id"]] = r["error_msg"] or ""
    return out


def count_bids(user_id: int, account_id: int) -> int:
    bsc = _bid_scoped_id(account_id)
    with _conn() as con:
        r = con.execute(
            "SELECT COUNT(*) AS n FROM bids WHERE user_id = %s AND bid_scoped = %s",
            (user_id, bsc),
        ).fetchone()
    return int(r["n"]) if r else 0


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def add_log(
    user_id: int, message: str, *, level: str = "info", account_id: int | None = None
) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO logs(user_id, account_id, level, message) VALUES(%s, %s, %s, %s)",
            (user_id, account_id, level, message),
        )


def list_logs(user_id: int, limit: int = 300) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT l.*, a.name AS account_name
            FROM logs l
            LEFT JOIN accounts a ON l.account_id = a.id AND a.user_id = l.user_id
            WHERE l.user_id = %s
            ORDER BY l.id DESC LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def clear_logs(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM logs WHERE user_id = %s", (user_id,))
