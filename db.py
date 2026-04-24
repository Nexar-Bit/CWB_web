"""
SQLite persistence layer for CrowdWorks Bot.

Tables
------
settings  — global key-value store (openai_api_key, scrape_interval, ...)
prompts   — reusable proposal-generation prompts
accounts  — CrowdWorks accounts: session cookie + assigned prompt
bids      — per-account bid submission records
logs      — bot activity log (info / success / warning / error)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from _appdir import APP_DIR

DB_PATH = APP_DIR / "crowdworks_bot.db"

_TABLES = """
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
    UNIQUE(account_id, job_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init() -> None:
    """Ensure the database file exists, create all tables, then apply migrations.

    On first launch (or if the file was deleted) the database is created from
    scratch.  Subsequent calls are idempotent — existing data is never touched.
    """
    is_new_db = not DB_PATH.exists()

    # Make sure the containing directory exists before SQLite tries to open the file.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(_TABLES)

        if is_new_db:
            # Commit the schema immediately so the file is valid even if the
            # process is interrupted before _migrate_from_json() runs.
            con.commit()

        # incremental migrations for the bids table
        existing_cols = {
            row[1]
            for row in con.execute("PRAGMA table_info(bids)").fetchall()
        }
        if "status" not in existing_cols:
            con.execute(
                "ALTER TABLE bids ADD COLUMN status TEXT NOT NULL DEFAULT 'success'"
            )
            con.commit()
        if "error_msg" not in existing_cols:
            con.execute(
                "ALTER TABLE bids ADD COLUMN error_msg TEXT NOT NULL DEFAULT ''"
            )
            con.commit()
    finally:
        con.close()

    if is_new_db:
        # Record the creation event so it appears in the activity log immediately
        # after the app finishes loading.
        add_log(
            f"Database created at {DB_PATH}",
            level="info",
        )

    _migrate_from_json()


def _migrate_from_json() -> None:
    """
    One-time migration from local_settings.json → SQLite.

    Runs only when the settings table is still empty (i.e. first launch after
    the upgrade to SQLite).  Transfers:
      * openai_api_key    → settings table
      * proposal_extra_prompt → prompts table as "Default Prompt"
      * cw_session_id     → accounts table as "Account 1" (with the prompt above)
    The source JSON file is left untouched.
    """
    import json as _json

    json_path = DB_PATH.parent / "local_settings.json"
    if not json_path.exists():
        return

    try:
        data: dict = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return

    # Only migrate when the DB is brand-new (nothing in settings yet)
    with _conn() as con:
        already_populated = con.execute(
            "SELECT 1 FROM settings LIMIT 1"
        ).fetchone()
    if already_populated:
        return

    old_key     = str(data.get("openai_api_key")        or "").strip()
    old_prompt  = str(data.get("proposal_extra_prompt") or "").strip()
    old_session = str(data.get("cw_session_id")         or "").strip()

    if old_key:
        set_setting("openai_api_key", old_key)

    # Create a "Default Prompt" only when the prompts table is empty
    with _conn() as con:
        prompt_count = con.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]

    prompt_id: int | None = None
    if old_prompt and prompt_count == 0:
        prompt_id = add_prompt("Default Prompt", old_prompt)

    # Create an account from the legacy single-session setup when none exist
    with _conn() as con:
        acc_count = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    if old_session and acc_count == 0:
        if prompt_id is None:
            existing = list_prompts()
            prompt_id = existing[0]["id"] if existing else None
        add_account("Account 1", old_session, prompt_id=prompt_id)

    # Write a sentinel so migration never runs again even if key/prompt are empty
    if not get_setting("_migrated"):
        set_setting("_migrated", "1")


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ── Prompts ───────────────────────────────────────────────────────────────────

def list_prompts() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM prompts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_prompt(prompt_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    return dict(row) if row else None


def add_prompt(name: str, content: str) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO prompts(name, content) VALUES(?,?)", (name, content)
        )
        rowid = cur.lastrowid
    return int(rowid)


def update_prompt(prompt_id: int, name: str, content: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE prompts SET name=?, content=? WHERE id=?",
            (name, content, prompt_id),
        )


def delete_prompt(prompt_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))


# ── Accounts ──────────────────────────────────────────────────────────────────

def list_accounts() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT a.id, a.name, a.session_id, a.cw_username,
                   a.prompt_id, a.enabled, a.status,
                   a.last_verified, a.created_at,
                   p.name    AS prompt_name,
                   p.content AS prompt_content
            FROM   accounts a
            LEFT JOIN prompts p ON a.prompt_id = p.id
            ORDER  BY a.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """
            SELECT a.*, p.name AS prompt_name, p.content AS prompt_content
            FROM   accounts a
            LEFT JOIN prompts p ON a.prompt_id = p.id
            WHERE  a.id = ?
            """,
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def add_account(
    name: str,
    session_id: str,
    *,
    prompt_id: int | None = None,
    enabled: int = 1,
) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO accounts(name, session_id, prompt_id, enabled) VALUES(?,?,?,?)",
            (name, session_id, prompt_id, enabled),
        )
        rowid = cur.lastrowid
    return int(rowid)


def update_account(account_id: int, **kwargs) -> None:
    _allowed = {
        "name", "session_id", "cw_username",
        "prompt_id", "enabled", "status", "last_verified",
    }
    fields = {k: v for k, v in kwargs.items() if k in _allowed}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [account_id]
    with _conn() as con:
        con.execute(f"UPDATE accounts SET {cols} WHERE id=?", vals)


def delete_account(account_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM accounts WHERE id=?", (account_id,))


# ── Bids ──────────────────────────────────────────────────────────────────────

def has_bid(account_id: int | None, job_id: str) -> bool:
    """Return True when the account already has a completed bid for this job.

    Both ``'success'`` and ``'already_bid'`` are treated as "done" — the bot
    must not attempt to submit again in either case.
    Only ``'failed'`` records allow re-submission / retry.
    """
    with _conn() as con:
        if account_id is None:
            row = con.execute(
                "SELECT 1 FROM bids"
                " WHERE account_id IS NULL AND job_id=? AND status IN ('success','already_bid')",
                (str(job_id),),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT 1 FROM bids"
                " WHERE account_id=? AND job_id=? AND status IN ('success','already_bid')",
                (account_id, str(job_id)),
            ).fetchone()
    return row is not None


def record_bid(
    job_id: str,
    *,
    job_title: str = "",
    result_url: str = "",
    account_id: int | None = None,
    status: str = "success",
    error_msg: str = "",
) -> None:
    """Insert or update a bid record.

    status values
    -------------
    ``'success'``     – proposal was accepted by the server.
    ``'already_bid'`` – submission skipped because the account already applied
                        (detected when the DB was missing and the bot tried again).
    ``'failed'``      – submission was attempted but the server rejected it or an
                        error occurred.

    Priority order for ON CONFLICT: success > already_bid > failed.
    A lower-priority status can never overwrite a higher-priority one.
    ``error_msg`` is stored only for failed rows and is cleared on upgrade.
    """
    with _conn() as con:
        con.execute(
            "INSERT INTO bids(account_id, job_id, job_title, result_url, status, error_msg)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(account_id, job_id) DO UPDATE SET"
            # Numeric priority: success=2, already_bid=1, failed=0
            "   status       = CASE"
            "     WHEN CASE excluded.status WHEN 'success' THEN 2 WHEN 'already_bid' THEN 1 ELSE 0 END"
            "        > CASE status          WHEN 'success' THEN 2 WHEN 'already_bid' THEN 1 ELSE 0 END"
            "     THEN excluded.status ELSE status END,"
            "   result_url   = CASE WHEN excluded.status='success'"
            "                       THEN excluded.result_url ELSE result_url END,"
            "   error_msg    = CASE WHEN excluded.status IN ('success','already_bid') THEN ''"
            "                       ELSE excluded.error_msg END,"
            "   submitted_at = CASE WHEN excluded.status IN ('success','already_bid')"
            "                       THEN datetime('now') ELSE submitted_at END",
            (account_id, str(job_id), job_title, result_url, status, error_msg),
        )


def delete_bid(account_id: int | None, job_id: str) -> None:
    with _conn() as con:
        if account_id is None:
            con.execute(
                "DELETE FROM bids WHERE account_id IS NULL AND job_id=?",
                (str(job_id),),
            )
        else:
            con.execute(
                "DELETE FROM bids WHERE account_id=? AND job_id=?",
                (account_id, str(job_id)),
            )


def get_bid_job_ids(account_id: int | None = None) -> set[str]:
    """Return job_ids that have *any* bid record (success or failed)."""
    with _conn() as con:
        if account_id is not None:
            rows = con.execute(
                "SELECT job_id FROM bids WHERE account_id=?", (account_id,)
            ).fetchall()
        else:
            rows = con.execute("SELECT DISTINCT job_id FROM bids").fetchall()
    return {r["job_id"] for r in rows}


def get_bid_statuses(account_id: int | None = None) -> dict[str, str]:
    """Return ``{job_id: status}`` for all bid records.

    When *account_id* is ``None`` (all-accounts view), the *best* status
    across every account is used per job.  Priority order (highest first):
    ``'success'`` > ``'already_bid'`` > ``'failed'``.

    Possible status values: ``'success'``, ``'already_bid'``, ``'failed'``.
    Jobs with no bid record are simply absent from the returned dict.
    """
    with _conn() as con:
        if account_id is not None:
            rows = con.execute(
                "SELECT job_id, status FROM bids WHERE account_id=?",
                (account_id,),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT job_id,
                          CASE
                            WHEN MAX(CASE status WHEN 'success'      THEN 2 ELSE 0 END) = 2
                                 THEN 'success'
                            WHEN MAX(CASE status WHEN 'already_bid'  THEN 1 ELSE 0 END) = 1
                                 THEN 'already_bid'
                            ELSE 'failed'
                          END AS status
                   FROM   bids
                   GROUP  BY job_id"""
            ).fetchall()
    return {r["job_id"]: r["status"] for r in rows}


def get_bid_errors(account_id: int | None = None) -> dict[str, str]:
    """Return ``{job_id: error_msg}`` for failed bids.

    When *account_id* is ``None`` the most recent error across all accounts
    is returned for each job.  Jobs with no failed record are absent.
    """
    with _conn() as con:
        if account_id is not None:
            rows = con.execute(
                "SELECT job_id, error_msg FROM bids"
                " WHERE account_id=? AND status='failed'"
                " ORDER BY submitted_at DESC",
                (account_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT job_id, error_msg FROM bids"
                " WHERE status='failed'"
                " ORDER BY submitted_at DESC",
            ).fetchall()
    result: dict[str, str] = {}
    for r in rows:
        jid = r["job_id"]
        if jid not in result:
            result[jid] = r["error_msg"] or ""
    return result


def count_bids(account_id: int) -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM bids WHERE account_id=?", (account_id,)
        ).fetchone()
    return int(row["n"]) if row else 0


def list_bids(account_id: int | None = None, limit: int = 500) -> list[dict]:
    with _conn() as con:
        if account_id is not None:
            rows = con.execute(
                """SELECT b.*, a.name AS account_name
                   FROM bids b LEFT JOIN accounts a ON b.account_id=a.id
                   WHERE b.account_id=? ORDER BY b.submitted_at DESC LIMIT ?""",
                (account_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT b.*, a.name AS account_name
                   FROM bids b LEFT JOIN accounts a ON b.account_id=a.id
                   ORDER BY b.submitted_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Logs ──────────────────────────────────────────────────────────────────────

def add_log(
    message: str,
    *,
    level: str = "info",
    account_id: int | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO logs(account_id, level, message) VALUES(?,?,?)",
            (account_id, level, message),
        )


def list_logs(limit: int = 300) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT l.*, a.name AS account_name
               FROM   logs l
               LEFT JOIN accounts a ON l.account_id = a.id
               ORDER  BY l.id DESC
               LIMIT  ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_logs() -> None:
    with _conn() as con:
        con.execute("DELETE FROM logs")
