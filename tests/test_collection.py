from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import make_posts

from igdigest.collector import LOCK_NAME, run_collection
from igdigest.providers.base import (
    LoginRequired,
    MediaRef,
    Provider,
    RawPost,
    TransientError,
)
from igdigest.repo import acquire_lock, add_account


def count(db, sql, *params):
    with db.read() as conn:
        return conn.execute(sql, params).fetchone()[0]


def account(db, username):
    with db.read() as conn:
        return conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()


def test_first_check_imports_at_most_five_latest_and_marks_initial(db, settings, fixture_file, no_sleep):
    fixture_file.set_account("alpha", make_posts("a", 8))
    add_account(db, "alpha")

    summary = run_collection(db, settings, sleep=no_sleep[1])

    assert summary.status == "ok"
    assert summary.results[0].is_initial and summary.results[0].new_count == 5
    with db.read() as conn:
        rows = conn.execute("SELECT shortcode, is_initial, is_fixture FROM items ORDER BY shortcode").fetchall()
    assert [r["shortcode"] for r in rows] == ["A4", "A5", "A6", "A7", "A8"]  # the 5 newest
    assert all(r["is_initial"] and r["is_fixture"] for r in rows)
    assert count(db, "SELECT is_initial FROM check_runs") == 1


def test_repeat_checks_deduplicate_and_collect_only_new_posts(db, settings, fixture_file, no_sleep):
    fixture_file.set_account("alpha", make_posts("a", 3))
    add_account(db, "alpha")
    run_collection(db, settings, sleep=no_sleep[1])
    second = run_collection(db, settings, sleep=no_sleep[1])
    assert second.results[0].new_count == 0
    assert count(db, "SELECT COUNT(*) FROM items") == 3

    fixture_file.add_post("alpha", {
        "id": "a-new", "shortcode": "ANEW", "published_at": datetime.now(timezone.utc).isoformat(),
        "caption": "fresh", "media": [],
    })
    third = run_collection(db, settings, sleep=no_sleep[1])
    assert third.results[0].new_count == 1 and not third.results[0].is_initial
    assert count(db, "SELECT COUNT(*) FROM items") == 4
    assert count(db, "SELECT is_initial FROM items WHERE shortcode = 'ANEW'") == 0


class ScriptedProvider(Provider):
    """Returns/raises a scripted sequence per username."""

    name = "scripted"

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def fetch_recent(self, username, limit):
        self.calls.append(username)
        step = self.script[username].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def download(self, media, dest_dir: Path, max_bytes):
        raise NotImplementedError


def post(post_id, shortcode, hours_ago=1):
    return RawPost(post_id=post_id, shortcode=shortcode, url=f"https://www.instagram.com/p/{shortcode}/",
                   published_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago), caption="c",
                   media=[MediaRef(kind="image", url=None, note="n/a")])


def test_same_post_from_different_providers_is_not_duplicated(db, settings, no_sleep):
    add_account(db, "alpha")
    # Graph API media id and web id differ; the shortcode is the shared stable key.
    run_collection(db, settings, provider=ScriptedProvider({"alpha": [[post("1789000111", "SAME1")]]}), sleep=no_sleep[1])
    run_collection(db, settings, provider=ScriptedProvider({"alpha": [[post("3456_web", "SAME1")]]}), sleep=no_sleep[1])
    assert count(db, "SELECT COUNT(*) FROM items") == 1


def test_login_required_stops_run_records_status_and_never_substitutes(db, settings, fixture_file, no_sleep):
    # Fixture data exists for every account; a failing live provider must not fall back to it.
    for u in ("alpha", "beta", "gamma"):
        fixture_file.set_account(u, make_posts(u, 2))
        add_account(db, u)
    provider = ScriptedProvider({
        "alpha": [[post("1", "A1")]],
        "beta": [LoginRequired("Instagram session expired. Run `igdigest login`.")],
        "gamma": [[post("3", "G1")]],
    })

    summary = run_collection(db, settings, provider=provider, sleep=no_sleep[1])

    assert summary.status == "stopped"
    assert provider.calls == ["alpha", "beta"]  # gamma never contacted
    statuses = {r.username: r.status for r in summary.results}
    assert statuses == {"alpha": "ok", "beta": "login_required", "gamma": "skipped"}
    assert account(db, "beta")["status"] == "login_required"
    assert "login" in account(db, "beta")["last_error"]
    assert account(db, "beta")["initial_import_done"] == 0  # first check still pending
    assert count(db, "SELECT COUNT(*) FROM items") == 1
    assert count(db, "SELECT COUNT(*) FROM items WHERE is_fixture = 1") == 0


def test_transient_errors_retry_with_exponential_backoff(db, settings, no_sleep):
    add_account(db, "alpha")
    provider = ScriptedProvider({"alpha": [TransientError("502"), TransientError("502"), [post("1", "A1")]]})
    delays, sleep = no_sleep

    summary = run_collection(db, settings, provider=provider, sleep=sleep)

    assert summary.results[0].status == "ok" and summary.results[0].attempts == 3
    assert delays == [5, 10]


def test_transient_errors_exhausted_mark_failure_and_back_off_scheduled_checks(db, settings, no_sleep):
    add_account(db, "alpha")
    provider = ScriptedProvider({"alpha": [TransientError("502")] * 3})
    summary = run_collection(db, settings, provider=provider, sleep=no_sleep[1])
    assert summary.results[0].status == "failed"
    acct = account(db, "alpha")
    assert acct["consecutive_failures"] == 1 and acct["next_attempt_at"]

    # A scheduled run respects the backoff window; a manual one does not.
    assert run_collection(db, settings, trigger="scheduled", provider=ScriptedProvider({}), sleep=no_sleep[1]).status == "no_accounts"


def test_overlapping_runs_are_skipped(db, settings, no_sleep):
    add_account(db, "alpha")
    assert acquire_lock(db, LOCK_NAME, 5)
    provider = ScriptedProvider({"alpha": [[post("1", "A1")]]})
    summary = run_collection(db, settings, provider=provider, sleep=no_sleep[1])
    assert summary.status == "skipped_overlap"
    assert provider.calls == []


def test_unknown_account_in_fixture_mode_fails_instead_of_getting_sample_data(db, settings, fixture_file, no_sleep):
    fixture_file.set_account("alpha", make_posts("a", 2))
    add_account(db, "someone_real")
    summary = run_collection(db, settings, sleep=no_sleep[1])
    assert summary.results[0].status == "not_accessible"
    assert count(db, "SELECT COUNT(*) FROM items") == 0
