"""
SQLite persistence: multi-tenant (user_id) CrowdWorks Bot data.

* users           — app login (web); id=1 is the desktop “local” user
* user_settings   — per-user key / value
* prompts, accounts, bids, logs — all scoped with user_id

Desktop code uses ``DESKTOP_USER_ID`` (1) for all calls. Web uses the signed-in user.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Generator

from _appdir import APP_DIR

DB_PATH = APP_DIR / "crowdworks_bot.db"

# Single-user / legacy desktop: always this row in ``users`` (email __local@desktop).
DESKTOP_USER_ID = 1


def _bid_scoped_id(account_id: int | None) -> int:
    return int(account_id) if account_id is not None else 0

_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _bootstrap_schema(con: sqlite3.Connection) -> None:
    """Initial schema: legacy tables, then we migrate in _migrate_tenant()."""
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS prompts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            content    TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL DEFAULT 'Account',
            session_id    TEXT NOT NULL DEFAULT '',
            cw_username   TEXT NOT NULL DEFAULT '',
            prompt_id     INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
            enabled       INTEGER NOT NULL DEFAULT 1,
            status        TEXT NOT NULL DEFAULT 'unverified',
            last_verified TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bids (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id   INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            job_id       TEXT NOT NULL,
            job_title    TEXT NOT NULL DEFAULT '',
            result_url   TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'success',
            error_msg    TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL DEFAULT (datetime('now')),
            bid_scoped   INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            level      TEXT NOT NULL DEFAULT 'info',
            message    TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """
    )


def init() -> None:
    is_new = not DB_PATH.exists()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(_TABLES)
        _bootstrap_schema(con)
        con.commit()
    finally:
        con.close()

    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        # bids columns from older dbs
        bcols = {r[1] for r in con.execute("PRAGMA table_info(bids)").fetchall()}
        if "status" not in bcols:
            con.execute("ALTER TABLE bids ADD COLUMN status TEXT NOT NULL DEFAULT 'success'")
        if "error_msg" not in bcols:
            con.execute("ALTER TABLE bids ADD COLUMN error_msg TEXT NOT NULL DEFAULT ''")
        if "bid_scoped" not in bcols:
            con.execute("ALTER TABLE bids ADD COLUMN bid_scoped INTEGER NOT NULL DEFAULT 0")
        con.commit()
    finally:
        con.close()

    _migrate_tenant()
    _ensure_local_desktop_user()
    _migrate_from_json()
    if is_new:
        add_log(DESKTOP_USER_ID, f"Database created at {DB_PATH}", level="info")


def _table_cols(con: sqlite3.Connection, name: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({name})").fetchall()}


def _migrate_tenant() -> None:
    """Add users, user_id columns, user_settings, rebuild bids uniques, backfill."""
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.executescript(_TABLES)
        con.execute(
            "INSERT OR IGNORE INTO users (id, email, password_hash) VALUES (1, '__local@desktop', '*')"
        )
        con.commit()
    except Exception:
        con.rollback()
    finally:
        con.close()

    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        for tbl in ("prompts", "accounts", "bids", "logs"):
            tcols = _table_cols(con, tbl)
            if "user_id" in tcols:
                continue
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER")
            con.execute(
                f"UPDATE {tbl} SET user_id = {DESKTOP_USER_ID} WHERE user_id IS NULL"
            )
        con.commit()
    except Exception:
        con.rollback()
    finally:
        con.close()

    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        tcols = _table_cols(con, "bids")
        if tcols and "user_id" in tcols:
            con.execute("UPDATE bids SET user_id=1 WHERE user_id IS NULL")
        if tcols and "bid_scoped" in tcols:
            con.execute("UPDATE bids SET bid_scoped=ifnull(account_id,0)")
        con.commit()
    except Exception:
        con.rollback()
    finally:
        con.close()

    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        uexists = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        if "idx_bids_user_scoped_job" not in uexists and "bids" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            try:
                con.execute("DROP INDEX IF EXISTS sqlite_autoindex_bids_1")
            except sqlite3.OperationalError:
                pass
            try:
                con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bids_user_scoped_job ON bids (user_id, bid_scoped, job_id)")
            except sqlite3.OperationalError:
                pass
        con.commit()
    except Exception:
        con.rollback()
    finally:
        con.close()

    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        tnames = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "user_settings" not in tnames:
            con.executescript(
                """
                CREATE TABLE user_settings (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    key     TEXT NOT NULL,
                    value   TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (user_id, key)
                );
            """
            )
        # migrate old global settings → user 1
        tnames2 = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "settings" in tnames2 and "user_settings" in tnames2:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
            for k, v in rows:
                con.execute(
                    "INSERT OR REPLACE INTO user_settings(user_id, key, value) VALUES(?,?,?)",
                    (DESKTOP_USER_ID, k, v),
                )
            con.execute("DROP TABLE settings")
        con.commit()
    except Exception:
        con.rollback()
    finally:
        con.close()


def _ensure_local_desktop_user() -> None:
    with _conn() as con:
        r = con.execute("SELECT id FROM users WHERE id=?", (DESKTOP_USER_ID,)).fetchone()
        if r:
            return
    with _conn() as con:
        con.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (1, '__local@desktop', '*')",
        )


def _migrate_from_json() -> None:
    import json as _json

    path = DB_PATH.parent / "local_settings.json"
    if not path.exists():
        return
    try:
        data: dict = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    with _conn() as con:
        if con.execute(
            f"SELECT 1 FROM user_settings WHERE user_id=? LIMIT 1",
            (DESKTOP_USER_ID,),
        ).fetchone():
            return
    old_key = str(data.get("openai_api_key") or "").strip()
    if old_key:
        set_setting(DESKTOP_USER_ID, "openai_api_key", old_key)
    old_session = str(data.get("cw_session_id") or "").strip()
    old_prompt = str(data.get("proposal_extra_prompt") or "").strip()
    pid: int | None = None
    with _conn() as con:
        pcount = con.execute("SELECT COUNT(*) FROM prompts WHERE user_id=?", (DESKTOP_USER_ID,)).fetchone()
        pcounti = pcount[0] if pcount else 0
    if old_prompt and pcounti == 0:
        pid = add_prompt(DESKTOP_USER_ID, "Default Prompt", old_prompt)
    with _conn() as con:
        acn = con.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (DESKTOP_USER_ID,)).fetchone()
        nacc = acn[0] if acn else 0
    if old_session and nacc == 0:
        if pid is None:
            pp = list_prompts(DESKTOP_USER_ID)
            pid = int(pp[0]["id"]) if pp else None
        add_account(DESKTOP_USER_ID, "Account 1", old_session, prompt_id=pid)
    if not get_setting(DESKTOP_USER_ID, "_migrated"):
        set_setting(DESKTOP_USER_ID, "_migrated", "1")


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ── Users (web + local row) ─────────────────────────────────────────────────

def user_register(email: str, password_hash: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO users(email, password_hash) VALUES(?,?)", (email, password_hash)
        )
        return int(cur.lastrowid or 0)


def user_by_email(email: str) -> dict | None:
    e = (email or "").strip().lower()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE lower(email)=?", (e,)).fetchone()
    return dict(row) if row else None


def user_by_id(uid: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def is_internal_desktop_user(uid: int) -> bool:
    if uid == DESKTOP_USER_ID:
        return True
    u = user_by_id(uid)
    return u is not None and (u.get("email") or "").lower() == "__local@desktop"


# ── Settings (per user) ────────────────────────────────────────────────────

def get_setting(user_id: int, key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?",
            (user_id, key),
        ).fetchone()
    return str(row["value"]) if row else default


def set_setting(user_id: int, key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO user_settings(user_id, key, value) VALUES(?,?,?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (user_id, key, value),
        )


# ── Prompts ─────────────────────────────────────────────────────────────────

def list_prompts(user_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM prompts WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_prompt(user_id: int, prompt_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM prompts WHERE id=? AND user_id=?", (prompt_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def add_prompt(user_id: int, name: str, content: str) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO prompts(user_id, name, content) VALUES(?,?,?)",
            (user_id, name, content),
        )
        return int(cur.lastrowid or 0)


def update_prompt(user_id: int, prompt_id: int, name: str, content: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE prompts SET name=?, content=? WHERE id=? AND user_id=?",
            (name, content, prompt_id, user_id),
        )


def delete_prompt(user_id: int, prompt_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM prompts WHERE id=? AND user_id=?", (prompt_id, user_id))


# ── Accounts ────────────────────────────────────────────────────────────────

def list_accounts(user_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT a.id, a.name, a.session_id, a.cw_username, a.user_id,
                   a.prompt_id, a.enabled, a.status, a.last_verified, a.created_at,
                   p.name AS prompt_name, p.content AS prompt_content
            FROM accounts a
            LEFT JOIN prompts p ON a.prompt_id = p.id
            WHERE a.user_id=?
            ORDER BY a.id
        """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_account(user_id: int, account_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """
            SELECT a.*, p.name AS prompt_name, p.content AS prompt_content
            FROM accounts a
            LEFT JOIN prompts p ON a.prompt_id = p.id
            WHERE a.id = ? AND a.user_id=?
        """,
            (account_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def add_account(
    user_id: int,
    name: str,
    session_id: str,
    *,
    prompt_id: int | None = None,
    enabled: int = 1,
) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO accounts(user_id, name, session_id, prompt_id, enabled) VALUES(?,?,?,?,?)",
            (user_id, name, session_id, prompt_id, enabled),
        )
        return int(cur.lastrowid or 0)


def update_account(user_id: int, account_id: int, **kwargs) -> None:
    allowed = {
        "name", "session_id", "cw_username", "prompt_id", "enabled", "status", "last_verified"
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [account_id, user_id]
    with _conn() as con:
        con.execute(
            f"UPDATE accounts SET {cols} WHERE id=? AND user_id=?", vals
        )


def delete_account(user_id: int, account_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, user_id))


# ── Bids ────────────────────────────────────────────────────────────────────

def has_bid(user_id: int, account_id: int | None, job_id: str) -> bool:
    bsc = _bid_scoped_id(account_id)
    with _conn() as con:
        row = con.execute(
            """
            SELECT 1 FROM bids
            WHERE user_id=? AND job_id=? AND bid_scoped=? AND status IN ('success','already_bid')
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
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, bid_scoped, job_id) DO UPDATE SET
            status = CASE
              WHEN CASE excluded.status
                   WHEN 'success' THEN 2
                   WHEN 'already_bid' THEN 1
                   ELSE 0 END
                 > CASE status
                   WHEN 'success' THEN 2
                   WHEN 'already_bid' THEN 1
                   ELSE 0 END
                THEN excluded.status
                ELSE status
              END,
            result_url = CASE
              WHEN excluded.status = 'success'
                THEN excluded.result_url
                ELSE result_url
              END,
            error_msg = CASE
              WHEN excluded.status IN ('success','already_bid')
                THEN ''
                ELSE excluded.error_msg
              END,
            submitted_at = CASE
              WHEN excluded.status IN ('success','already_bid')
                THEN datetime('now')
                ELSE submitted_at
              END,
            job_title = excluded.job_title
        """,
            (
                user_id,
                account_id,
                bsc,
                job_id,
                job_title,
                result_url,
                status,
                error_msg,
            ),
        )


def delete_bid(user_id: int, account_id: int | None, job_id: str) -> None:
    bsc = _bid_scoped_id(account_id)
    with _conn() as con:
        con.execute(
            "DELETE FROM bids WHERE user_id=? AND job_id=? AND bid_scoped=?",
            (user_id, str(job_id), bsc),
        )


def get_bid_job_ids(user_id: int, account_id: int | None = None) -> set[str]:
    with _conn() as con:
        if account_id is not None:
            bsc = _bid_scoped_id(account_id)
            rows = con.execute(
                "SELECT job_id FROM bids WHERE user_id=? AND bid_scoped=?",
                (user_id, bsc),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT job_id FROM bids WHERE user_id=?", (user_id,)
            ).fetchall()
    return {r["job_id"] for r in rows}


def get_bid_statuses(user_id: int, account_id: int | None) -> dict[str, str]:
    with _conn() as con:
        if account_id is not None:
            bsc = _bid_scoped_id(account_id)
            rows = con.execute(
                "SELECT job_id, status FROM bids WHERE user_id=? AND bid_scoped=?",
                (user_id, bsc),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT job_id, CASE
                  WHEN MAX(CASE status WHEN 'success'      THEN 2 ELSE 0 END) = 2
                       THEN 'success'
                  WHEN MAX(CASE status WHEN 'already_bid'  THEN 1 ELSE 0 END) = 1
                       THEN 'already_bid'
                  ELSE 'failed'
                END AS status
                FROM bids
                WHERE user_id=?
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
                "SELECT job_id, error_msg FROM bids WHERE user_id=? AND bid_scoped=? AND status='failed' ORDER BY submitted_at DESC",
                (user_id, bsc),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT job_id, error_msg FROM bids WHERE user_id=? AND status='failed' ORDER BY submitted_at DESC",
                (user_id,),
            ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if r["job_id"] not in out:
            out[r["job_id"]] = (r["error_msg"] or "") or ""
    return out


def count_bids(user_id: int, account_id: int) -> int:
    bsc = _bid_scoped_id(account_id)
    with _conn() as con:
        r = con.execute(
            "SELECT COUNT(*) n FROM bids WHERE user_id=? AND bid_scoped=?",
            (user_id, bsc),
        ).fetchone()
    return int(r["n"] or 0) if r else 0


# ── Logs ───────────────────────────────────────────────────────────────────

def add_log(
    user_id: int, message: str, *, level: str = "info", account_id: int | None = None
) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO logs(user_id, account_id, level, message) VALUES(?,?,?,?)",
            (user_id, account_id, level, message),
        )


def list_logs(user_id: int, limit: int = 300) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT l.*, a.name AS account_name
            FROM logs l
            LEFT JOIN accounts a ON l.account_id = a.id AND a.user_id = l.user_id
            WHERE l.user_id=?
            ORDER BY l.id DESC LIMIT ?
        """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_logs(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM logs WHERE user_id=?", (user_id,))
