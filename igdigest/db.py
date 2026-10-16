"""SQLite storage: connection handling and versioned migrations (PRAGMA user_version)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Each entry is one migration. Never edit an applied migration; append a new one.
MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        provider TEXT,                                  -- per-account override; NULL = default
        paused INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',             -- new|ok|failed|login_required|blocked|not_accessible
        added_at TEXT NOT NULL,
        last_checked_at TEXT,
        last_success_at TEXT,
        last_error TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        initial_import_done INTEGER NOT NULL DEFAULT 0,
        initial_import_at TEXT
    );

    CREATE TABLE check_runs (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        trigger TEXT NOT NULL,                          -- manual|scheduled
        provider TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,                           -- running|ok|failed|login_required|blocked|not_accessible|skipped
        is_initial INTEGER NOT NULL DEFAULT 0,
        fetched_count INTEGER NOT NULL DEFAULT 0,
        new_count INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        notes TEXT
    );
    CREATE INDEX ix_check_runs_account ON check_runs(account_id, started_at);
    CREATE INDEX ix_check_runs_started ON check_runs(started_at);

    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        post_key TEXT NOT NULL,                         -- stable key: shortcode, else id:<provider id>
        provider_post_id TEXT,
        shortcode TEXT,
        provider TEXT NOT NULL,
        is_fixture INTEGER NOT NULL DEFAULT 0,
        url TEXT NOT NULL,
        media_type TEXT,
        is_reel INTEGER NOT NULL DEFAULT 0,
        published_at TEXT,
        caption TEXT,
        collected_at TEXT NOT NULL,
        check_run_id INTEGER REFERENCES check_runs(id) ON DELETE SET NULL,
        is_initial INTEGER NOT NULL DEFAULT 0,
        extraction_status TEXT NOT NULL DEFAULT 'pending', -- pending|full|partial|caption_only|none
        ocr_text TEXT,
        transcript TEXT,
        extraction_notes TEXT,
        UNIQUE (account_id, post_key)
    );
    CREATE INDEX ix_items_collected ON items(collected_at);

    CREATE TABLE digests (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,                             -- scheduled|preview
        trigger TEXT NOT NULL,                          -- scheduled|manual
        status TEXT NOT NULL,                           -- ok|failed
        created_at TEXT NOT NULL,
        window_start TEXT,
        window_end TEXT NOT NULL,
        language TEXT NOT NULL,
        summarizer TEXT NOT NULL,
        item_count INTEGER NOT NULL DEFAULT 0,
        is_fixture INTEGER NOT NULL DEFAULT 0,
        content_json TEXT,
        content_md TEXT,
        error TEXT
    );

    CREATE TABLE digest_items (
        digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        PRIMARY KEY (digest_id, item_id)
    );

    CREATE TABLE locks (
        name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """,
    # 2: "updates" reports (new-post lists, no LLM), tracked independently from LLM digests
    """
    CREATE TABLE update_reports (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,                             -- scheduled (marks delivered) | preview
        trigger TEXT NOT NULL,                          -- scheduled|manual
        status TEXT NOT NULL,                           -- ok|failed
        created_at TEXT NOT NULL,
        window_start TEXT,
        window_end TEXT NOT NULL,
        item_count INTEGER NOT NULL DEFAULT 0,
        is_fixture INTEGER NOT NULL DEFAULT 0,
        content_json TEXT,
        content_md TEXT,
        error TEXT
    );

    CREATE TABLE update_report_items (
        report_id INTEGER NOT NULL REFERENCES update_reports(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        PRIMARY KEY (report_id, item_id)
    );
    """,
    # 3: Telegram bot - chats, per-chat delivery log, polling offset
    """
    CREATE TABLE telegram_chats (
        chat_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        username TEXT,
        subscribed INTEGER NOT NULL DEFAULT 1,
        subscribed_at TEXT NOT NULL,
        state TEXT,                                     -- pending conversation step, e.g. await_add
        last_failure_sig TEXT                           -- failures in the last sent no-new-posts report
    );

    CREATE TABLE telegram_deliveries (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
        source TEXT NOT NULL,                           -- update_report|digest
        source_id INTEGER NOT NULL,
        status TEXT NOT NULL,                           -- pending|sent|skipped|failed
        parts_sent INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        error TEXT,
        UNIQUE (chat_id, source, source_id)
    );

    CREATE TABLE telegram_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # 4: display details for messages - account name/picture, post cover image
    """
    ALTER TABLE accounts ADD COLUMN full_name TEXT;
    ALTER TABLE accounts ADD COLUMN profile_pic_url TEXT;
    ALTER TABLE items ADD COLUMN thumbnail_url TEXT;
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """Fixed-width UTC timestamps so string comparison in SQL matches time order."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One connection, one IMMEDIATE transaction; commits on success."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> int:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version, script in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(script):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {version}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()


def _split_sql(script: str) -> list[str]:
    # Strip "--" comments first; migrations contain no "--" or ";" inside string literals.
    code = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    return [stmt for stmt in code.split(";") if stmt.strip()]


def init_db(path: Path) -> Database:
    db = Database(path)
    db.migrate()
    return db
