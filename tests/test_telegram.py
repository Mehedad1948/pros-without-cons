"""Telegram bot: access control, account management flows, and update delivery (fake Bot API)."""

import json

import httpx
import pytest
from conftest import make_posts

from igdigest.collector import run_collection
from igdigest.repo import add_account, list_accounts
from igdigest.telegram.api import MAX_MESSAGE_CHARS, TelegramAPI, TelegramError, TelegramNetworkError
from igdigest.telegram.bot import BTN_ADD, BTN_CHECK, Bot
from igdigest.telegram.delivery import deliver_pending
from datetime import datetime, timedelta, timezone

from igdigest.telegram.render import CAPTION_LIMIT, ago, updates_messages, visible_len
from igdigest.updates import build_updates_report

OWNER = 42


class FakeAPI:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []
        self.fail_on = {}  # send index -> exception

    def send_message(self, chat_id, text, reply_markup=None, preview=False):
        idx = len(self.sent)
        if idx in self.fail_on:
            raise self.fail_on.pop(idx)
        assert len(text) <= 4096
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return {}

    def send_photo(self, chat_id, photo_url, caption, reply_markup=None):
        idx = len(self.sent)
        if idx in self.fail_on:
            raise self.fail_on.pop(idx)
        self.sent.append({"chat_id": chat_id, "text": caption, "photo": photo_url, "markup": reply_markup})
        return {}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "text": text, "markup": reply_markup})

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    def texts(self):
        return [m["text"] for m in self.sent]


@pytest.fixture
def tg_settings(settings):
    return settings.model_copy(update={"telegram_allowed_user_ids": str(OWNER)})


@pytest.fixture
def bot(db, tg_settings):
    return Bot(db, tg_settings, FakeAPI(), runner=lambda fn: fn(), sleep=lambda s: None)


def message(text, user=OWNER):
    return {"update_id": 1, "message": {"chat": {"id": user, "type": "private"}, "from": {"id": user}, "text": text}}


def callback(data, user=OWNER):
    return {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": user}, "data": data,
                                               "message": {"message_id": 7, "chat": {"id": user, "type": "private"}}}}


def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def test_strangers_are_refused_and_told_their_id(bot, db):
    bot.handle_update(message("/add someone", user=999))
    bot.handle_update(callback("add:someone", user=999))
    assert "999" in bot.api.sent[0]["text"] and "private bot" in bot.api.sent[0]["text"]
    assert bot.api.answers == ["Not allowed"]
    assert list_accounts(db) == []
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM telegram_chats").fetchone()[0] == 0


def test_start_shows_menu_and_subscribes(bot, db):
    bot.handle_update(message("/start"))
    assert "keyboard" in bot.api.sent[0]["markup"]
    with db.read() as conn:
        assert conn.execute("SELECT subscribed FROM telegram_chats WHERE chat_id = ?", (OWNER,)).fetchone()[0] == 1


def test_add_button_then_send_usernames(bot, db):
    bot.handle_update(message(BTN_ADD))
    assert "username" in bot.api.sent[-1]["text"]
    bot.handle_update(message("https://www.instagram.com/alpha/ @beta https://www.instagram.com/p/XYZ/"))
    reply = bot.api.sent[-1]["text"]
    assert "@alpha" in reply and "@beta" in reply and "not a profile" in reply
    assert [a["username"] for a in list_accounts(db)] == ["alpha", "beta"]


def test_pasted_profile_link_offers_add_with_confirmation(bot, db):
    bot.handle_update(message("look at instagram.com/gamma?igsh=1"))  # link inside a sentence
    offer = bot.api.sent[-1]
    assert [b["callback_data"] for b in buttons(offer["markup"])] == ["add:gamma", "cancel"]
    assert list_accounts(db) == []
    bot.handle_update(callback("add:gamma"))
    assert [a["username"] for a in list_accounts(db)] == ["gamma"]
    assert "Now monitoring" in bot.api.edits[-1]["text"]


def test_account_menu_pause_and_remove_with_confirmation(bot, db):
    row, _ = add_account(db, "alpha")
    bot.handle_update(message("/list"))
    assert f"acct:{row['id']}" in [b["callback_data"] for b in buttons(bot.api.sent[-1]["markup"])]

    bot.handle_update(callback(f"pause:{row['id']}"))
    assert list_accounts(db)[0]["paused"] == 1
    assert f"resume:{row['id']}" in [b["callback_data"] for b in buttons(bot.api.edits[-1]["markup"])]

    bot.handle_update(callback(f"del:{row['id']}"))
    assert "Remove" in bot.api.edits[-1]["text"] and len(list_accounts(db)) == 1  # only asks
    bot.handle_update(callback(f"delok:{row['id']}"))
    assert list_accounts(db) == [] and bot.api.answers[-1] == "Removed @alpha"


def test_check_now_sends_new_posts_as_messages(bot, db, fixture_file):
    fixture_file.set_account("alpha", make_posts("a", 2))
    fixture_file.data["accounts"]["alpha"]["posts"][0]["caption"] = "<b>bold?</b> & more"
    fixture_file.save()
    add_account(db, "alpha")
    bot.handle_update(message("/start"))
    bot.handle_update(message(BTN_CHECK))

    texts = bot.api.texts()
    report = next(t for t in texts if "@alpha" in t and "new posts" in t)
    assert report.startswith("📣 <b>alpha</b>") and "initial recap" in report
    assert 'href="https://example.com/igdigest-fixture/alpha/A1"' in report
    assert "&lt;b&gt;bold?&lt;/b&gt; &amp; more" in report  # third-party text is escaped
    assert "sample data" in report
    assert any(t.startswith("✅ <b>Check finished</b>: 2 new post(s)") for t in texts)

    # Already delivered: nothing is sent twice.
    before = len(bot.api.sent)
    assert bot.deliver() == 0 and len(bot.api.sent) == before
    bot.handle_update(message("/updates"))
    assert "Nothing new" in bot.api.sent[-1]["text"]


def subscribe(db, chat_id=OWNER):
    with db.tx() as conn:
        conn.execute("INSERT INTO telegram_chats (chat_id, user_id, subscribed_at) VALUES (?, ?, '2000-01-01')",
                     (chat_id, chat_id))


def test_long_reports_are_split_and_resume_after_a_failed_part(db, tg_settings, fixture_file, no_sleep):
    long_posts = make_posts("a", 40)
    for p in long_posts:
        p["caption"] = "x" * 290
    fixture_file.set_account("alpha", long_posts)
    add_account(db, "alpha")
    run_collection(db, tg_settings.model_copy(update={"initial_import_limit": 40, "max_posts_per_check": 40}),
                   sleep=no_sleep[1])
    report_id = build_updates_report(db, tg_settings)
    with db.read() as conn:
        parts = updates_messages(json.loads(conn.execute(
            "SELECT content_json FROM update_reports WHERE id = ?", (report_id,)).fetchone()[0]))
    assert len(parts) >= 3 and all(len(p["text"]) <= MAX_MESSAGE_CHARS and p["photo"] is None for p in parts)

    subscribe(db)
    api = FakeAPI()
    api.fail_on = {1: TelegramNetworkError("sendMessage", None, "network error")}
    assert deliver_pending(db, tg_settings, api, sleep=lambda s: None) == 1
    assert deliver_pending(db, tg_settings, api, sleep=lambda s: None) == len(parts) - 1
    assert api.texts() == [p["text"] for p in parts]  # every part exactly once, in order
    assert deliver_pending(db, tg_settings, api, sleep=lambda s: None) == 0


def test_repeated_failure_alerts_are_sent_once(db, tg_settings, fixture_file, no_sleep):
    fixture_file.set_account("down", simulate="transient")
    add_account(db, "down")
    subscribe(db)
    api = FakeAPI()
    for _ in range(2):
        run_collection(db, tg_settings, sleep=no_sleep[1])
        build_updates_report(db, tg_settings, skip_if_empty=True)
        deliver_pending(db, tg_settings, api, sleep=lambda s: None)
    assert len(api.sent) == 1 and "@down" in api.sent[0]["text"] and "Check problems" in api.sent[0]["text"]


def test_blocked_chat_is_unsubscribed(db, tg_settings, fixture_file, no_sleep):
    fixture_file.set_account("alpha", make_posts("a", 1))
    add_account(db, "alpha")
    run_collection(db, tg_settings, sleep=no_sleep[1])
    build_updates_report(db, tg_settings)
    subscribe(db)
    api = FakeAPI()
    api.fail_on = {0: TelegramError("sendMessage", 403, "Forbidden: bot was blocked by the user")}
    deliver_pending(db, tg_settings, api, sleep=lambda s: None)
    with db.read() as conn:
        assert conn.execute("SELECT subscribed FROM telegram_chats").fetchone()[0] == 0


def test_api_client_redacts_token_and_long_polls():
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen[req.url.path.rsplit("/", 1)[-1]] = body
        if req.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(401, json={"ok": False, "error_code": 401,
                                         "description": f"Unauthorized {req.url.path}"})

    api = TelegramAPI("123:SECRET", transport=httpx.MockTransport(handler))
    assert api.get_updates(5, 25) == []
    assert seen["getUpdates"] == {"offset": 5, "timeout": 25, "allowed_updates": ["message", "callback_query"]}
    with pytest.raises(TelegramError) as exc:
        api.send_message(1, "hi")
    assert "SECRET" not in str(exc.value) and exc.value.code == 401
    assert seen["sendMessage"]["parse_mode"] == "HTML"


def test_relative_times():
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    tz = timezone.utc
    def at(**kw):
        return (now - timedelta(**kw)).isoformat()
    assert ago(at(seconds=20), now, tz) == "just now"
    assert ago(at(minutes=4), now, tz) == "4 min ago"
    assert ago(at(hours=2, minutes=59), now, tz) == "2 h ago"
    assert ago(at(hours=30), now, tz) == "yesterday"
    assert ago(at(days=3), now, tz) == "3 days ago"
    assert ago(at(days=20), now, tz) == "26 Sep"
    assert ago(None, now, tz) == "time unknown"


def report_content(accounts, **extra):
    return {"timezone": "UTC", "is_fixture": False, "accounts": accounts, "coverage": {"failed": []},
            "notes": [], "status_message": None, **extra}


def post(code, minutes_ago, thumb=None, caption="A caption"):
    now = datetime.now(timezone.utc)
    return {"url": f"https://www.instagram.com/p/{code}/", "type": "image", "caption": caption,
            "published_at": (now - timedelta(minutes=minutes_ago)).isoformat(), "published": "x",
            "thumbnail_url": thumb}


def test_one_message_per_account_with_photo_bold_name_and_relative_time():
    content = report_content([
        {"username": "nasa", "full_name": "NASA", "initial": False,
         "posts": [post("N1", 4, thumb="https://cdn.example/n1.jpg"), post("N2", 125)]},
        {"username": "bbcnews", "full_name": None, "profile_pic_url": "https://cdn.example/bbc.jpg",
         "initial": True, "posts": [post("B1", 30)]},
    ], coverage={"failed": [{"username": "433", "status": "blocked", "status_text": "collection blocked",
                             "error": "HTTP 429", "latest_failed": True}]})
    msgs = updates_messages(content)
    assert len(msgs) == 3  # nasa, bbcnews, check problems
    nasa, bbc, problems = msgs
    assert nasa["photo"] == "https://cdn.example/n1.jpg" and nasa["text"].startswith("📣 <b>NASA</b>\n@nasa")
    assert "4 min ago" in nasa["text"] and "2 h ago" in nasa["text"] and "@bbcnews" not in nasa["text"]
    assert bbc["photo"] == "https://cdn.example/bbc.jpg" and "<b>bbcnews</b>" in bbc["text"]  # profile picture fallback
    assert problems["photo"] is None and "Check problems" in problems["text"] and "@433" in problems["text"]


def test_captions_shrink_to_fit_and_overflow_continues_as_text():
    few = report_content([{"username": "a", "initial": False,
                           "posts": [post(f"P{i}", i, thumb="https://cdn.example/x.jpg", caption="y" * 300) for i in range(5)]}])
    msgs = updates_messages(few)
    assert len(msgs) == 1 and visible_len(msgs[0]["text"]) <= CAPTION_LIMIT and msgs[0]["text"].count("Open on Instagram") == 5

    many = report_content([{"username": "a", "initial": False,
                            "posts": [post(f"P{i}", i, thumb="https://cdn.example/x.jpg", caption="y" * 300) for i in range(25)]}])
    msgs = updates_messages(many)
    assert msgs[0]["photo"] and visible_len(msgs[0]["text"]) <= CAPTION_LIMIT
    assert all(m["photo"] is None for m in msgs[1:]) and "continued" in msgs[1]["text"]
    assert sum(m["text"].count("Open on Instagram") for m in msgs) == 25


def test_photo_rejected_by_telegram_is_sent_as_text(db, tg_settings):
    from igdigest.telegram.delivery import send_part

    api = FakeAPI()
    api.fail_on = {0: TelegramError("sendPhoto", 400, "Bad Request: failed to get HTTP URL content")}
    send_part(api, OWNER, {"text": "<b>NASA</b>", "photo": "https://cdn.example/expired.jpg"})
    assert api.sent == [{"chat_id": OWNER, "text": "<b>NASA</b>", "markup": None}]
