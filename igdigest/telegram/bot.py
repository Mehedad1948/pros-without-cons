"""Menu-driven Telegram bot: manage monitored accounts and receive new posts as messages.

Only Telegram user IDs listed in IGD_TELEGRAM_ALLOWED_USER_IDS may use it, in private chats.
Uses long polling (getUpdates), so no public URL or webhook is needed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Callable

from ..collector import LOCK_NAME, RunSummary, run_collection
from ..config import Settings
from ..db import Database, iso, utcnow
from ..reporting import fmt_local
from ..repo import (
    InvalidAccount,
    acquire_lock,
    add_account,
    find_account,
    list_accounts,
    lock_held,
    parse_username,
    release_lock,
    remove_account,
    set_paused,
)
from ..updates import UpdatesBusy, build_updates_report
from .api import TelegramAPI, TelegramError
from .delivery import deliver_pending, send_part
from .render import account_icon, digest_messages, esc, link, updates_messages

log = logging.getLogger(__name__)

BTN_ADD = "➕ Add account"
BTN_LIST = "\U0001f4cb My accounts"
BTN_CHECK = "\U0001f504 Check now"
BTN_NEW = "\U0001f4f0 New posts"
BTN_STATUS = "ℹ️ Status"
BTN_HELP = "❓ Help"
BUTTONS = {BTN_ADD: "add", BTN_LIST: "list", BTN_CHECK: "check", BTN_NEW: "updates",
           BTN_STATUS: "status", BTN_HELP: "help"}

MENU = {
    "keyboard": [[{"text": BTN_ADD}, {"text": BTN_LIST}],
                 [{"text": BTN_CHECK}, {"text": BTN_NEW}],
                 [{"text": BTN_STATUS}, {"text": BTN_HELP}]],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Send an Instagram username or profile link",
}

COMMANDS = [
    ("start", "Show the menu and receive updates"),
    ("add", "Add an Instagram account"),
    ("list", "Show monitored accounts"),
    ("remove", "Remove an account"),
    ("check", "Check all accounts now"),
    ("updates", "Send new posts now"),
    ("digest", "LLM recap preview (if enabled)"),
    ("status", "Collection and bot status"),
    ("stop", "Stop receiving updates"),
    ("help", "How to use this bot"),
]

HELP = (
    "<b>How to use this bot</b>\n\n"
    f"{BTN_ADD}: send a username, <code>@username</code> or a profile link "
    "(<code>instagram.com/name</code>). You can also just paste a profile link at any time.\n"
    f"{BTN_LIST}: tap an account to pause, resume, check or remove it.\n"
    f"{BTN_CHECK}: check all accounts now; new posts arrive here.\n"
    f"{BTN_NEW}: send any collected posts you have not received yet.\n\n"
    "New posts are also sent automatically after each scheduled check. The first check of a new "
    "account sends up to its 5 latest posts as an <i>initial recap</i>.\n\n"
    "/stop pauses messages, /start resumes them. Broadcast channels are not supported; add the creator's profile."
)


def _ig_ref(text: str) -> bool:
    return "instagram.com/" in text.lower() or bool(re.fullmatch(r"@[A-Za-z0-9._]{1,30}", text.strip()))


class Bot:
    def __init__(self, db: Database, settings: Settings, api: TelegramAPI,
                 runner: Callable[[Callable[[], None]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.db, self.settings, self.api = db, settings, api
        self.allowed = settings.telegram_allowed_ids
        self.runner = runner or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.sleep = sleep

    # ------------------------------------------------------------------ helpers
    def send(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        try:
            self.api.send_message(chat_id, text, reply_markup=markup)
        except TelegramError as e:
            log.warning("Telegram send to %s failed: %s", chat_id, e)

    def _send_part(self, chat_id: int, part: dict) -> None:
        try:
            send_part(self.api, chat_id, part)
        except TelegramError as e:
            log.warning("Telegram send to %s failed: %s", chat_id, e)

    def _chat(self, chat_id: int):
        with self.db.read() as conn:
            return conn.execute("SELECT * FROM telegram_chats WHERE chat_id = ?", (chat_id,)).fetchone()

    def _ensure_chat(self, chat_id: int, user: dict):
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO telegram_chats (chat_id, user_id, username, subscribed_at) VALUES (?, ?, ?, ?)",
                (chat_id, user.get("id"), user.get("username"), iso(utcnow())),
            )
        return self._chat(chat_id)

    def _set_state(self, chat_id: int, state: str | None) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE telegram_chats SET state = ? WHERE chat_id = ?", (state, chat_id))

    def _set_subscribed(self, chat_id: int, on: bool) -> None:
        with self.db.tx() as conn:
            if on:
                conn.execute(
                    "UPDATE telegram_chats SET subscribed_at = CASE WHEN subscribed = 0 THEN ? ELSE subscribed_at END, "
                    "subscribed = 1 WHERE chat_id = ?", (iso(utcnow()), chat_id))
            else:
                conn.execute("UPDATE telegram_chats SET subscribed = 0 WHERE chat_id = ?", (chat_id,))

    def deliver(self, wait: bool = False) -> int:
        """Push owed reports to all subscribers; one deliverer at a time across processes."""
        for _ in range(15 if wait else 1):
            owner = acquire_lock(self.db, "telegram_delivery", 10)
            if owner:
                try:
                    return deliver_pending(self.db, self.settings, self.api, sleep=self.sleep)
                finally:
                    release_lock(self.db, "telegram_delivery", owner)
            if wait:
                self.sleep(1)
        return 0

    # ------------------------------------------------------------------ dispatch
    def handle_update(self, update: dict) -> None:
        if "message" in update:
            self._on_message(update["message"])
        elif "callback_query" in update:
            self._on_callback(update["callback_query"])

    def _deny(self, chat_id: int, user_id: int | None) -> None:
        self.send(chat_id,
                  "⛔ This is a private bot.\n"
                  f"Your Telegram user ID is <code>{user_id}</code>. If this is your bot, add that ID to "
                  "<code>IGD_TELEGRAM_ALLOWED_USER_IDS</code> and restart it.")

    def _on_message(self, msg: dict) -> None:
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        chat_id = chat.get("id")
        if chat.get("type") != "private":
            self.send(chat_id, "Please talk to me in a private chat.")
            return
        if user.get("id") not in self.allowed:
            self._deny(chat_id, user.get("id"))
            return
        row = self._ensure_chat(chat_id, user)
        text = (msg.get("text") or "").strip()
        if not text:
            self.send(chat_id, "Send me an Instagram username or profile link, or use the menu.", MENU)
            return

        cmd, arg = None, ""
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            cmd = head[1:].split("@")[0].lower()
            arg = arg.strip()
        elif text in BUTTONS:
            cmd = BUTTONS[text]

        if cmd:
            if row["state"]:
                self._set_state(chat_id, None)
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler is None:
                self.send(chat_id, "Unknown command. Use the menu below or /help.", MENU)
            else:
                handler(chat_id, arg)
        elif row["state"] == "await_add":
            self._set_state(chat_id, None)
            self._add_accounts(chat_id, text)
        elif _ig_ref(text):
            self._offer_add(chat_id, text)
        else:
            self.send(chat_id, "I didn't understand that. Send an Instagram profile link, or use the menu below.", MENU)

    def _on_callback(self, cq: dict) -> None:
        user = cq.get("from") or {}
        msg = cq.get("message") or {}
        chat_id, message_id = (msg.get("chat") or {}).get("id"), msg.get("message_id")
        data = cq.get("data") or ""
        if user.get("id") not in self.allowed:
            self._answer(cq, "Not allowed")
            return
        action, _, value = data.partition(":")
        toast = None
        try:
            if action == "add":
                row, created = add_account(self.db, value)
                text = (f"✅ Now monitoring <b>@{esc(row['username'])}</b>." if created
                        else f"@{esc(row['username'])} is already monitored.")
                self._edit(chat_id, message_id, text + "\nIts first check sends up to 5 latest posts as an initial recap.",
                           {"inline_keyboard": [[{"text": "\U0001f50d Check it now", "callback_data": f"check:{row['id']}"}]]})
            elif action == "cancel":
                self._edit(chat_id, message_id, "Cancelled.")
            elif action == "addprompt":
                self.cmd_add(chat_id, "")
            elif action == "list":
                self._edit(chat_id, message_id, *self._accounts_view())
            elif action == "acct":
                self._edit(chat_id, message_id, *self._account_view(int(value)))
            elif action in ("pause", "resume"):
                set_paused(self.db, int(value), action == "pause")
                toast = "Paused" if action == "pause" else "Resumed"
                self._edit(chat_id, message_id, *self._account_view(int(value)))
            elif action == "del":
                self._edit(chat_id, message_id, *self._confirm_remove(int(value)))
            elif action == "delok":
                acct = self._account(int(value))
                if acct and remove_account(self.db, acct["id"]):
                    toast = f"Removed @{acct['username']}"
                self._edit(chat_id, message_id, *self._accounts_view())
            elif action == "check":
                acct = self._account(int(value))
                if acct:
                    self._start_check(chat_id, [acct["username"]])
            elif action == "checkall":
                self._start_check(chat_id, None)
        except InvalidAccount as e:
            toast = str(e)[:190]
        self._answer(cq, toast)

    def _answer(self, cq: dict, text: str | None) -> None:
        try:
            self.api.answer_callback_query(cq["id"], text)
        except TelegramError as e:
            log.debug("answerCallbackQuery failed: %s", e)

    def _edit(self, chat_id, message_id, text: str, markup: dict | None = None) -> None:
        try:
            self.api.edit_message_text(chat_id, message_id, text, markup)
        except TelegramError:
            self.send(chat_id, text, markup)  # message too old to edit: send a new one

    # ------------------------------------------------------------------ commands
    def cmd_start(self, chat_id: int, arg: str) -> None:
        self._set_subscribed(chat_id, True)
        n = len(list_accounts(self.db))
        self.send(chat_id,
                  "\U0001f44b <b>Instagram updates</b>\n\n"
                  f"I watch {n} Instagram account(s) for you and send new posts here.\n"
                  "Use the menu below to add or manage accounts. /help explains everything.", MENU)

    def cmd_help(self, chat_id: int, arg: str) -> None:
        self.send(chat_id, HELP, MENU)

    def cmd_stop(self, chat_id: int, arg: str) -> None:
        self._set_subscribed(chat_id, False)
        self.send(chat_id, "\U0001f515 You will no longer receive updates here. Send /start to resume.")

    def cmd_cancel(self, chat_id: int, arg: str) -> None:
        self.send(chat_id, "OK, cancelled.", MENU)

    def cmd_add(self, chat_id: int, arg: str) -> None:
        if arg:
            self._add_accounts(chat_id, arg)
            return
        self._set_state(chat_id, "await_add")
        self.send(chat_id,
                  "Send the Instagram <b>username</b> or <b>profile link</b> to add.\n"
                  "Several at once: separate them with spaces or new lines. /cancel to stop.")

    def cmd_list(self, chat_id: int, arg: str) -> None:
        self.send(chat_id, *self._accounts_view())

    def cmd_remove(self, chat_id: int, arg: str) -> None:
        if arg:
            with self.db.read() as conn:
                acct = find_account(conn, arg)
            if not acct:
                self.send(chat_id, f"@{esc(arg.lstrip('@'))} is not monitored.")
                return
            self.send(chat_id, *self._confirm_remove(acct["id"]))
        else:
            text, markup = self._accounts_view()
            self.send(chat_id, text + "\n\nTap an account, then \U0001f5d1 Remove.", markup)

    def cmd_check(self, chat_id: int, arg: str) -> None:
        if arg:
            with self.db.read() as conn:
                acct = find_account(conn, arg)
            if not acct:
                self.send(chat_id, f"@{esc(arg.lstrip('@'))} is not monitored. Add it first.")
                return
            self._start_check(chat_id, [acct["username"]])
        else:
            self._start_check(chat_id, None)

    def cmd_updates(self, chat_id: int, arg: str) -> None:
        if not self.settings.updates_enabled:
            self.send(chat_id, "Updates are turned off (IGD_UPDATES_ENABLED=false).")
            return
        try:
            report_id = build_updates_report(self.db, self.settings, trigger="manual", skip_if_empty=True)
        except UpdatesBusy:
            self.send(chat_id, "⏳ A report is being prepared right now; try again in a moment.")
            return
        if report_id is None:
            self.send(chat_id, "✨ Nothing new since your last update.")
            return
        self.deliver(wait=True)
        self._ensure_received(chat_id, report_id)

    def cmd_digest(self, chat_id: int, arg: str) -> None:
        if not self.settings.digest_available:
            self.send(chat_id, "The LLM digest is off: no LLM is configured (IGD_LLM_PROVIDER). "
                               f"{BTN_NEW} works without it.")
            return
        self.send(chat_id, "\U0001f9e0 Writing a digest preview… this can take a minute.")

        def job():
            from ..digest import DigestBusy, DigestUnavailable, build_digest

            try:
                digest_id = build_digest(self.db, self.settings, kind="preview", trigger="manual")
            except (DigestBusy, DigestUnavailable) as e:
                self.send(chat_id, esc(str(e)))
                return
            with self.db.read() as conn:
                row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
            if row["status"] != "ok":
                self.send(chat_id, f"❌ Digest failed: {esc(row['error'])}")
                return
            for part in digest_messages(json.loads(row["content_json"])):
                self._send_part(chat_id, part)
                self.sleep(0.5)

        self.runner(job)

    def cmd_status(self, chat_id: int, arg: str) -> None:
        s = self.settings
        accounts = list_accounts(self.db)
        counts: dict[str, int] = {}
        for a in accounts:
            key = "paused" if a["paused"] else a["status"]
            counts[key] = counts.get(key, 0) + 1
        last_ok = max((a["last_success_at"] for a in accounts if a["last_success_at"]), default=None)
        chat = self._chat(chat_id)
        lines = [
            "ℹ️ <b>Status</b>",
            f"Collection: <b>{esc(s.provider)}</b>{' (fixture sample data)' if s.is_fixture_mode else ''}, "
            f"every {s.check_interval_hours:g} h while the app is running",
            f"Accounts: {len(accounts)} — " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"),
            f"Last successful check: {fmt_local(last_ok, s.tz) if last_ok else 'never'} ({esc(s.timezone)})",
            f"Check running now: {'yes' if lock_held(self.db, LOCK_NAME) else 'no'}",
            "LLM digest: " + (f"on, daily at {s.digest_time}" if s.digest_available else "off (no LLM configured)"),
            "This chat: " + ("receiving updates ✅ (/stop to pause)" if chat["subscribed"] else "paused \U0001f515 (/start to resume)"),
        ]
        self.send(chat_id, "\n".join(lines), MENU)

    # ------------------------------------------------------------------ flows
    def _add_accounts(self, chat_id: int, text: str) -> None:
        added, existing, errors = [], [], []
        for token in re.split(r"[\s,]+", text.strip()):
            if not token:
                continue
            try:
                row, created = add_account(self.db, token)
                (added if created else existing).append(row)
            except InvalidAccount as e:
                errors.append(str(e))
        lines = []
        if added:
            lines.append("✅ Now monitoring: " + ", ".join(f"<b>@{esc(r['username'])}</b>" for r in added))
            lines.append("Their first check sends up to 5 latest posts as an initial recap.")
        if existing:
            lines.append("Already monitored: " + ", ".join(f"@{esc(r['username'])}" for r in existing))
        lines += [f"⚠️ {esc(e)}" for e in errors]
        markup = None
        if added:
            markup = {"inline_keyboard": [[{"text": "\U0001f504 Check now", "callback_data": "checkall"},
                                           {"text": "\U0001f4cb My accounts", "callback_data": "list"}]]}
        self.send(chat_id, "\n".join(lines) or "Nothing to add.", markup)

    def _offer_add(self, chat_id: int, text: str) -> None:
        tokens = text.split()
        ref = next((t for t in tokens if "instagram.com/" in t.lower()), None) or \
            next((t for t in tokens if t.startswith("@")), tokens[0])
        try:
            username = parse_username(ref)
        except InvalidAccount as e:
            self.send(chat_id, f"⚠️ {esc(str(e))}")
            return
        self.send(chat_id, f"Add <b>@{esc(username)}</b> to your monitored accounts?",
                  {"inline_keyboard": [[{"text": f"✅ Add @{username}", "callback_data": f"add:{username}"},
                                        {"text": "✖️ Cancel", "callback_data": "cancel"}]]})

    def _start_check(self, chat_id: int, usernames: list[str] | None) -> None:
        if lock_held(self.db, LOCK_NAME):
            self.send(chat_id, "⏳ A check is already running. New posts will arrive here when it finishes.")
            return
        what = ", ".join(f"@{esc(u)}" for u in usernames) if usernames else "all active accounts"
        self.send(chat_id, f"\U0001f504 Checking {what}… new posts will arrive here.")

        def job():
            try:
                summary = run_collection(self.db, self.settings, trigger="manual", usernames=usernames)
            except Exception:
                log.exception("Telegram-triggered check crashed")
                self.send(chat_id, "❌ The check crashed; see the app log.")
                return
            if summary.status == "skipped_overlap":
                self.send(chat_id, "⏳ Another check was already running.")
                return
            report_id = None
            if self.settings.updates_enabled:
                try:
                    report_id = build_updates_report(self.db, self.settings, trigger="manual", skip_if_empty=True)
                except UpdatesBusy:
                    pass
                self.deliver(wait=True)
                if report_id:
                    self._ensure_received(chat_id, report_id, quiet_if_skipped=True)
            self.send(chat_id, self._summary_text(summary))

        self.runner(job)

    def _ensure_received(self, chat_id: int, report_id: int, quiet_if_skipped: bool = False) -> None:
        """If delivery did not send this report to this chat (paused chat, repeat alert), show it directly."""
        with self.db.read() as conn:
            d = conn.execute(
                "SELECT status FROM telegram_deliveries WHERE chat_id = ? AND source = 'update_report' AND source_id = ?",
                (chat_id, report_id),
            ).fetchone()
            row = conn.execute("SELECT content_json FROM update_reports WHERE id = ?", (report_id,)).fetchone()
        if d and d["status"] == "sent":
            return
        if quiet_if_skipped and d and d["status"] == "skipped":
            return
        for part in updates_messages(json.loads(row["content_json"])):
            self._send_part(chat_id, part)
            self.sleep(0.5)

    def _summary_text(self, summary: RunSummary) -> str:
        icon = {"ok": "✅", "partial": "⚠️", "failed": "❌", "stopped": "⛔"}.get(summary.status, "ℹ️")
        lines = [f"{icon} <b>Check finished</b>: {summary.new_count} new post(s)"]
        for r in summary.results:
            if r.status == "ok":
                extra = " (initial recap)" if r.is_initial else ""
                lines.append(f"• @{esc(r.username)}: {r.new_count} new{extra}")
            else:
                lines.append(f"• @{esc(r.username)}: <b>{esc(r.status.replace('_', ' '))}</b> — {esc(r.error or '')}")
        if summary.status == "stopped":
            lines.append("\nCollection stopped early. If login is required, run <code>igdigest login</code> on your computer.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ views
    def _account(self, account_id: int):
        with self.db.read() as conn:
            return find_account(conn, account_id)

    def _accounts_view(self) -> tuple[str, dict]:
        rows = list_accounts(self.db)
        tz = self.settings.tz
        if not rows:
            return ("\U0001f4cb <b>No accounts yet.</b>\nTap ➕ to add one.",
                    {"inline_keyboard": [[{"text": "➕ Add account", "callback_data": "addprompt"}]]})
        lines = [f"\U0001f4cb <b>Monitored accounts</b> ({len(rows)})", ""]
        for r in rows:
            state = "paused" if r["paused"] else r["status"].replace("_", " ")
            when = fmt_local(r["last_checked_at"], tz) if r["last_checked_at"] else "not checked yet"
            lines.append(f"{account_icon(r)} <b>@{esc(r['username'])}</b> — {esc(state)} · {r['item_count']} posts · {when}")
        buttons = [{"text": f"{account_icon(r)} @{r['username']}", "callback_data": f"acct:{r['id']}"} for r in rows[:96]]
        keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        keyboard.append([{"text": "➕ Add", "callback_data": "addprompt"},
                         {"text": "\U0001f504 Check all", "callback_data": "checkall"}])
        return "\n".join(lines), {"inline_keyboard": keyboard}

    def _account_view(self, account_id: int) -> tuple[str, dict]:
        r = self._account(account_id)
        if not r:
            return self._accounts_view()
        tz = self.settings.tz
        with self.db.read() as conn:
            items = conn.execute("SELECT COUNT(*) FROM items WHERE account_id = ?", (account_id,)).fetchone()[0]
        lines = [
            f"{account_icon(r)} <b>@{esc(r['username'])}</b>",
            f"Status: {'paused' if r['paused'] else esc(r['status'].replace('_', ' '))}",
            f"Last check: {fmt_local(r['last_checked_at'], tz) if r['last_checked_at'] else 'never'}",
            f"Last success: {fmt_local(r['last_success_at'], tz) if r['last_success_at'] else 'never'}",
            f"Collected posts: {items}",
        ]
        if r["last_error"]:
            lines.append(f"Last error: {esc(r['last_error'])}")
        if not self.settings.is_fixture_mode:
            lines.append(link(f"https://www.instagram.com/{r['username']}/", "Open profile"))
        toggle = ({"text": "▶️ Resume", "callback_data": f"resume:{r['id']}"} if r["paused"]
                  else {"text": "⏸ Pause", "callback_data": f"pause:{r['id']}"})
        keyboard = [
            [toggle, {"text": "\U0001f50d Check now", "callback_data": f"check:{r['id']}"}],
            [{"text": "\U0001f5d1 Remove", "callback_data": f"del:{r['id']}"},
             {"text": "« All accounts", "callback_data": "list"}],
        ]
        return "\n".join(lines), {"inline_keyboard": keyboard}

    def _confirm_remove(self, account_id: int) -> tuple[str, dict]:
        r = self._account(account_id)
        if not r:
            return self._accounts_view()
        with self.db.read() as conn:
            items = conn.execute("SELECT COUNT(*) FROM items WHERE account_id = ?", (account_id,)).fetchone()[0]
        return (f"Remove <b>@{esc(r['username'])}</b>?\nThis stops monitoring it and deletes its {items} collected post(s).",
                {"inline_keyboard": [[{"text": "\U0001f5d1 Yes, remove", "callback_data": f"delok:{r['id']}"},
                                      {"text": "✖️ Keep it", "callback_data": f"acct:{r['id']}"}]]})

    # ------------------------------------------------------------------ polling loop
    def _offset(self) -> int | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT value FROM telegram_state WHERE key = 'offset'").fetchone()
        return int(row["value"]) if row else None

    def _save_offset(self, offset: int) -> None:
        with self.db.tx() as conn:
            conn.execute("INSERT INTO telegram_state (key, value) VALUES ('offset', ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(offset),))

    def run(self, stop: threading.Event) -> bool:
        """Poll until `stop` is set. Returns False if the bot could not run (bad token, unreachable)."""
        try:
            me = self.api.get_me()
        except TelegramError as e:
            log.error("Telegram bot cannot start: %s", e)
            return False
        log.info("Telegram bot @%s running (%d allowed user(s))", me.get("username"), len(self.allowed))
        if not self.allowed:
            log.warning("IGD_TELEGRAM_ALLOWED_USER_IDS is empty: the bot refuses everyone and tells them their user ID.")
        try:
            self.api.set_my_commands(COMMANDS)
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e)

        offset, backoff, last_delivery = self._offset(), 5.0, 0.0
        while not stop.is_set():
            if time.monotonic() - last_delivery >= 20:
                try:
                    self.deliver()
                except Exception:
                    log.exception("Telegram delivery crashed")
                last_delivery = time.monotonic()
            try:
                updates = self.api.get_updates(offset, self.settings.telegram_poll_timeout)
                backoff = 5.0
            except TelegramError as e:
                if e.code == 401:
                    log.error("Telegram rejected the bot token; stopping the bot.")
                    return False
                if e.code == 409:
                    log.warning("Another process is polling this bot (409). Run the bot in only one process.")
                    stop.wait(30)
                    continue
                log.warning("Telegram polling error: %s; retrying in %.0fs", e, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("Error while handling a Telegram update")
            if updates:
                self._save_offset(offset)
        return True


def build_bot(db: Database, settings: Settings) -> Bot:
    if not settings.telegram_bot_token or not settings.telegram_bot_token.get_secret_value():
        raise ValueError("Set IGD_TELEGRAM_BOT_TOKEN (from @BotFather) to run the Telegram bot.")
    api = TelegramAPI(settings.telegram_bot_token.get_secret_value(), proxy=settings.telegram_proxy)
    return Bot(db, settings, api)


def start_bot_thread(db: Database, settings: Settings) -> tuple[threading.Thread, threading.Event]:
    bot = build_bot(db, settings)
    stop = threading.Event()
    thread = threading.Thread(target=bot.run, args=(stop,), name="telegram-bot", daemon=True)
    thread.start()
    return thread, stop
