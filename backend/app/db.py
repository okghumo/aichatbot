"""SQLite for the feedback loop: the one module that touches the database.

stdlib sqlite3, one short-lived connection per call (thread-safe, cheap on a local file), WAL mode.
Tables: feedback (CRM inbox), prompts (LIVE editable prompts), prompt_history (old prompts: at most
N per name, D days), prompt_drafts (proposed edits awaiting a human), response_log (which prompt
versions produced each reply). Moving to Postgres later means rewriting only this file and
prompt_store.py's SQL.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL,
    category     TEXT NOT NULL,
    session_id   TEXT,
    request_id   TEXT,
    response_id  TEXT,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'received',
    ref          TEXT NOT NULL,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS prompts (
    name       TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS prompt_drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    content      TEXT NOT NULL,
    reason       TEXT,
    feedback_id  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   TEXT,
    base_version INTEGER
);
CREATE TABLE IF NOT EXISTS response_log (
    response_id          TEXT PRIMARY KEY,
    session_id           TEXT,
    request_id           TEXT,
    created_at           TEXT NOT NULL,
    prompt_versions_json TEXT
);
"""

_initialised: set[str] = set()


def _connect() -> sqlite3.Connection:
    path = Path(settings.db_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)   # autocommit; tx() opens explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    if str(path) not in _initialised:
        conn.executescript(SCHEMA)
        try:   # ponytail: ad-hoc migration for DBs created before base_version existed; drop once all envs are rebuilt
            conn.execute("ALTER TABLE prompt_drafts ADD COLUMN base_version INTEGER")
        except sqlite3.OperationalError:
            pass   # column already there
        _initialised.add(str(path))
    return conn


@contextmanager
def tx():
    """One connection, one transaction: commits on success, rolls back on any error."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()
