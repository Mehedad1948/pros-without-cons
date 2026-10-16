"""Push stored reports to subscribed Telegram chats, exactly once per chat, resumable.

Sources: updates reports that marked posts as delivered (kind='scheduled') and, if enabled,
successful scheduled LLM digests. A chat only receives reports created after it subscribed.
A report with no new posts is only sent when the set of failing accounts changed, so a
persistent failure is announced once instead of every few hours.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from ..config import Settings
from ..db import Database, iso, utcnow
from .api import TelegramAPI, TelegramError, TelegramNetworkError
from .render import digest_messages, failure_signature, updates_messages

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
_CHAT_GONE = ("bot was blocked", "chat not found", "user is deactivated", "bot was kicked")


def send_part(api: TelegramAPI, chat_id: int, part: dict) -> None:
    """Send one rendered part: a photo with caption when possible, otherwise text.

    Instagram image links are fetched by Telegram's servers; if Telegram cannot use the image
    (expired link, unsupported file), the same content is sent as a text message instead.
    """
    if part.get("photo"):
        try:
            api.send_photo(chat_id, part["photo"], part["text"])
            return
        except TelegramError as e:
            if isinstance(e, TelegramNetworkError) or e.code in (401, 403, 429):
                raise
            log.info("Telegram did not accept the image (%s); sending text instead", e.description)
    api.send_message(chat_id, part["text"])


def _pending(conn, chat, settings: Settings) -> list[tuple[str, dict]]:
    done = "('sent', 'skipped')"
    rows = [("update_report", r) for r in conn.execute(
        f"""SELECT * FROM update_reports r WHERE r.kind = 'scheduled' AND r.status = 'ok' AND r.created_at >= ?
            AND NOT EXISTS (SELECT 1 FROM telegram_deliveries d WHERE d.chat_id = ? AND d.source = 'update_report'
                            AND d.source_id = r.id AND (d.status IN {done} OR d.attempts >= ?))""",
        (chat["subscribed_at"], chat["chat_id"], MAX_ATTEMPTS),
    )]
    if settings.telegram_send_digests:
        rows += [("digest", r) for r in conn.execute(
            f"""SELECT * FROM digests r WHERE r.kind = 'scheduled' AND r.status = 'ok' AND r.created_at >= ?
                AND NOT EXISTS (SELECT 1 FROM telegram_deliveries d WHERE d.chat_id = ? AND d.source = 'digest'
                                AND d.source_id = r.id AND (d.status IN {done} OR d.attempts >= ?))""",
            (chat["subscribed_at"], chat["chat_id"], MAX_ATTEMPTS),
        )]
    rows.sort(key=lambda x: x[1]["created_at"])
    return [(src, dict(r)) for src, r in rows]


def _record(db, chat_id, source, source_id, status, parts_sent, error=None, attempt=False):
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO telegram_deliveries (chat_id, source, source_id, status, parts_sent, attempts, updated_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (chat_id, source, source_id) DO UPDATE SET status = excluded.status,
                   parts_sent = excluded.parts_sent, attempts = attempts + ?, updated_at = excluded.updated_at,
                   error = excluded.error""",
            (chat_id, source, source_id, status, parts_sent, int(attempt), iso(utcnow()), error, int(attempt)),
        )


def deliver_pending(db: Database, settings: Settings, api: TelegramAPI,
                    sleep: Callable[[float], None] = time.sleep) -> int:
    """Send everything owed to every subscribed chat. Returns the number of messages sent."""
    sent_total = 0
    with db.read() as conn:
        chats = conn.execute("SELECT * FROM telegram_chats WHERE subscribed = 1").fetchall()
    for chat in chats:
        chat_id = chat["chat_id"]
        last_sig = chat["last_failure_sig"] or ""
        with db.read() as conn:
            pending = _pending(conn, chat, settings)
            parts_done = {
                (r["source"], r["source_id"]): r["parts_sent"]
                for r in conn.execute("SELECT * FROM telegram_deliveries WHERE chat_id = ?", (chat_id,))
            }
        for source, row in pending:
            content = json.loads(row["content_json"])
            if source == "update_report":
                sig = failure_signature(content)
                if row["item_count"] == 0 and sig == last_sig:
                    _record(db, chat_id, source, row["id"], "skipped", 0)
                    continue
                messages = updates_messages(content)
            else:
                sig = None
                messages = digest_messages(content)
            done = parts_done.get((source, row["id"]), 0)
            try:
                for i in range(done, len(messages)):
                    if i > 0 or sent_total:
                        sleep(0.5)  # stay well under per-chat flood limits
                    send_part(api, chat_id, messages[i])
                    done += 1
                    sent_total += 1
                _record(db, chat_id, source, row["id"], "sent", done)
                if sig is not None:
                    last_sig = sig
                    with db.tx() as conn:
                        conn.execute("UPDATE telegram_chats SET last_failure_sig = ? WHERE chat_id = ?", (sig, chat_id))
            except TelegramError as e:
                _record(db, chat_id, source, row["id"], "pending", done, str(e), attempt=True)
                if e.code == 403 or any(m in e.description.lower() for m in _CHAT_GONE):
                    log.warning("Telegram chat %s is gone (%s); unsubscribing", chat_id, e.description)
                    with db.tx() as conn:
                        conn.execute("UPDATE telegram_chats SET subscribed = 0 WHERE chat_id = ?", (chat_id,))
                    break
                if e.code == 429 or isinstance(e, TelegramNetworkError):
                    log.warning("Telegram delivery paused: %s", e)
                    return sent_total  # retry on the next cycle
                log.warning("Telegram delivery of %s #%s to %s failed: %s", source, row["id"], chat_id, e)
    return sent_total
