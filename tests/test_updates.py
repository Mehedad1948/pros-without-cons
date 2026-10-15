"""Updates (new-post reports) work without any LLM and independently of digests."""

import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from conftest import make_posts

from igdigest.collector import run_collection
from igdigest.digest import build_digest
from igdigest.llm import DigestDraft
from igdigest.repo import add_account
from igdigest.scheduler import create_scheduler, scheduled_collect
from igdigest.updates import build_updates_report


def report(db, report_id):
    with db.read() as conn:
        row = conn.execute("SELECT * FROM update_reports WHERE id = ?", (report_id,)).fetchone()
    return row, json.loads(row["content_json"])


def urls(content):
    return {p["url"] for a in content["accounts"] for p in a["posts"]}


@pytest.fixture
def collected(db, settings, fixture_file, no_sleep):
    fixture_file.set_account("alpha", make_posts("a", 2))
    fixture_file.set_account("beta", make_posts("b", 1))
    add_account(db, "alpha")
    add_account(db, "beta")
    run_collection(db, settings, sleep=no_sleep[1])
    return fixture_file


def test_updates_list_new_posts_without_llm(db, settings, collected):
    assert settings.llm_provider == "none"
    row, content = report(db, build_updates_report(db, settings, kind="preview"))
    assert row["item_count"] == 3 and content["is_fixture"]
    assert urls(content) == {f"https://example.com/igdigest-fixture/{u}/{c}" for u, c in
                             [("alpha", "A1"), ("alpha", "A2"), ("beta", "B1")]}
    assert "a caption 2" in row["content_md"] and "[open](" in row["content_md"]
    assert all(a["initial"] for a in content["accounts"])


def test_updates_preview_does_not_consume_and_report_does(db, settings, collected, no_sleep):
    build_updates_report(db, settings, kind="preview")
    first, _ = report(db, build_updates_report(db, settings, kind="scheduled"))
    assert first["item_count"] == 3

    collected.add_post("beta", {"id": "b-new", "shortcode": "BNEW", "caption": "new one",
                                "published_at": datetime.now(timezone.utc).isoformat(), "media": []})
    run_collection(db, settings, sleep=no_sleep[1])
    row, content = report(db, build_updates_report(db, settings, kind="scheduled"))
    assert urls(content) == {"https://example.com/igdigest-fixture/beta/BNEW"}


def test_updates_and_digests_track_delivery_independently(db, settings, collected):
    class LLM:
        label = "fake"

        def summarize(self, posts, window_text, language):
            return DigestDraft(overview="o", points=[])

    build_updates_report(db, settings, kind="scheduled")  # all 3 delivered as updates
    digest_id = build_digest(db, settings, kind="scheduled", summarizer=LLM())
    with db.read() as conn:
        assert conn.execute("SELECT item_count FROM digests WHERE id = ?", (digest_id,)).fetchone()[0] == 3
    # ...and the digest did not consume anything on the updates side either
    row, _ = report(db, build_updates_report(db, settings, kind="preview"))
    assert row["item_count"] == 0


def test_scheduled_check_writes_updates_only_when_there_is_something(db, settings, collected, no_sleep):
    scheduled_collect(db, settings)  # posts already collected but not yet reported -> report
    scheduled_collect(db, settings)  # nothing new, no failures -> no report
    with db.read() as conn:
        rows = conn.execute("SELECT item_count, trigger FROM update_reports").fetchall()
    assert [(r["item_count"], r["trigger"]) for r in rows] == [(3, "scheduled")]


def test_scheduler_registers_digest_job_only_with_an_llm(db, settings):
    assert [j.id for j in create_scheduler(db, settings).get_jobs()] == ["collect"]
    with_llm = settings.model_copy(update={"llm_provider": "anthropic"})
    assert {j.id for j in create_scheduler(db, with_llm).get_jobs()} == {"collect", "digest"}


def test_collection_and_updates_never_import_llm_code():
    code = (
        "import sys, igdigest.collector, igdigest.updates, igdigest.scheduler; "
        "assert 'igdigest.llm' not in sys.modules and 'igdigest.digest' not in sys.modules; "
        "assert 'anthropic' not in sys.modules and 'openai' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
