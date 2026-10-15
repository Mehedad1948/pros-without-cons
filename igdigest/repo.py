"""Data access helpers for accounts, check runs, items and locks."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from .db import Database, iso, utcnow

_USERNAME_RE = re.compile(r"^(?!\.)[a-z0-9._]{1,30}$")
# First path segments that are not profiles.
_NON_PROFILE_SEGMENTS = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "channel", "channels", "about", "developer", "legal", "web",
}


class InvalidAccount(ValueError):
    pass


def parse_username(value: str) -> str:
    """Accept a username, @username or an instagram.com profile URL; return the username."""
    raw = value.strip()
    if not raw:
        raise InvalidAccount("Empty username.")
    if raw.startswith("@"):
        raw = raw[1:]
    if "/" in raw or "instagram.com" in raw.lower() or "ig.me" in raw.lower():
        url = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host == "ig.me" or host.endswith(".ig.me"):
            raise InvalidAccount(
                "That looks like a broadcast channel invite link. Broadcast channels are not "
                "supported in this MVP; add the creator's profile instead."
            )
        if not (host == "instagram.com" or host.endswith(".instagram.com")):
            raise InvalidAccount(f"Not an Instagram URL: {value!r}")
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise InvalidAccount(f"No username in URL: {value!r}")
        first = parts[0].lower()
        if first in ("channel", "channels"):
            raise InvalidAccount(
                "Broadcast channels are not supported in this MVP; add the creator's profile instead."
            )
        if first in _NON_PROFILE_SEGMENTS:
            raise InvalidAccount(f"That is a post/reel/page link, not a profile: {value!r}")
        raw = parts[0]
    username = raw.lower()
    if not _USERNAME_RE.match(username):
        raise InvalidAccount(f"Not a valid Instagram username: {value!r}")
    return username


# --- accounts ---------------------------------------------------------------

def add_account(db: Database, value: str) -> tuple[sqlite3.Row, bool]:
    username = parse_username(value)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO accounts (username, added_at) VALUES (?, ?)",
            (username, iso(utcnow())),
        )
        created = cur.rowcount == 1
        row = conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
    return row, created


def find_account(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row | None:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(ref),)).fetchone()
        if row:
            return row
    try:
        username = parse_username(str(ref))
    except InvalidAccount:
        return None
    return conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()


def remove_account(db: Database, ref: str | int) -> bool:
    with db.tx() as conn:
        row = find_account(conn, ref)
        if not row:
            return False
        conn.execute("DELETE FROM accounts WHERE id = ?", (row["id"],))
        return True


def set_paused(db: Database, ref: str | int, paused: bool) -> bool:
    with db.tx() as conn:
        row = find_account(conn, ref)
        if not row:
            return False
        conn.execute("UPDATE accounts SET paused = ? WHERE id = ?", (int(paused), row["id"]))
        return True


def list_accounts(db: Database) -> list[sqlite3.Row]:
    with db.read() as conn:
        return conn.execute(
            """
            SELECT a.*,
                   (SELECT COUNT(*) FROM items i WHERE i.account_id = a.id) AS item_count
            FROM accounts a ORDER BY a.username
            """
        ).fetchall()


# --- locks (cross-process: CLI and web server share the database) ------------

def acquire_lock(db: Database, name: str, ttl_minutes: float) -> str | None:
    owner = uuid.uuid4().hex
    now = utcnow()
    with db.tx() as conn:
        conn.execute("DELETE FROM locks WHERE name = ? AND expires_at < ?", (name, iso(now)))
        cur = conn.execute(
            "INSERT OR IGNORE INTO locks (name, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (name, owner, iso(now), iso(now + timedelta(minutes=ttl_minutes))),
        )
        return owner if cur.rowcount == 1 else None


def refresh_lock(db: Database, name: str, owner: str, ttl_minutes: float) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE locks SET expires_at = ? WHERE name = ? AND owner = ?",
            (iso(utcnow() + timedelta(minutes=ttl_minutes)), name, owner),
        )


def release_lock(db: Database, name: str, owner: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))


def lock_held(db: Database, name: str) -> bool:
    with db.read() as conn:
        row = conn.execute(
            "SELECT 1 FROM locks WHERE name = ? AND expires_at >= ?", (name, iso(utcnow()))
        ).fetchone()
        return row is not None
