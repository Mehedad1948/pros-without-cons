"""Collection runs: check accounts, deduplicate posts, extract text, record outcomes."""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .config import Settings
from .db import Database, iso, parse_iso, utcnow
from .extraction import Extractor
from .providers import build_provider
from .providers.base import Provider, ProviderError, RawPost
from .repo import acquire_lock, refresh_lock, release_lock

log = logging.getLogger(__name__)

LOCK_NAME = "collect"


@dataclass
class AccountResult:
    username: str
    status: str
    provider: str
    new_count: int = 0
    fetched_count: int = 0
    is_initial: bool = False
    attempts: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    status: str  # ok | partial | failed | stopped | skipped_overlap | no_accounts
    run_id: str | None = None
    message: str = ""
    results: list[AccountResult] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return sum(r.new_count for r in self.results)


def run_collection(
    db: Database,
    settings: Settings,
    *,
    trigger: str = "manual",
    usernames: list[str] | None = None,
    provider: Provider | None = None,
    extractor: Extractor | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    """Check accounts once. Never overlaps with another run (cross-process DB lock)."""
    owner = acquire_lock(db, LOCK_NAME, settings.lock_ttl_minutes)
    if not owner:
        return RunSummary(status="skipped_overlap", message="Another check is already running; skipped.")
    try:
        return _run(db, settings, owner, trigger, usernames, provider, extractor, sleep)
    finally:
        release_lock(db, LOCK_NAME, owner)


def _run(db, settings, owner, trigger, usernames, provider, extractor, sleep) -> RunSummary:
    run_id = uuid.uuid4().hex[:12]
    now = utcnow()
    with db.read() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY username").fetchall()
    if usernames:
        wanted = {u.lower() for u in usernames}
        rows = [r for r in rows if r["username"].lower() in wanted]
    else:
        rows = [r for r in rows if not r["paused"]]
        if trigger == "scheduled":
            # Back off from accounts that keep failing; manual checks ignore this.
            rows = [r for r in rows if not r["next_attempt_at"] or parse_iso(r["next_attempt_at"]) <= now]
    if not rows:
        return RunSummary(status="no_accounts", run_id=run_id, message="No accounts to check.")

    extractor = extractor or Extractor(settings)
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        name = "injected" if provider else (r["provider"] or settings.provider)
        groups.setdefault(name, []).append(r)

    summary = RunSummary(status="ok", run_id=run_id)
    stop_reason: str | None = None

    for name, accounts in groups.items():
        prov: Provider | None = provider
        try:
            if prov is None:
                prov = build_provider(settings, name)
            prov.open()
        except ProviderError as e:
            for acct in accounts:
                summary.results.append(_record_failure(db, settings, run_id, trigger, acct, name, e, 0))
            if e.stops_run:
                stop_reason = stop_reason or str(e)
            continue
        try:
            for i, acct in enumerate(accounts):
                if stop_reason:
                    summary.results.append(_record_skipped(db, run_id, trigger, acct, prov.name, stop_reason))
                    continue
                if i > 0 and prov.pace_seconds:
                    sleep(prov.pace_seconds)
                refresh_lock(db, LOCK_NAME, owner, settings.lock_ttl_minutes)
                result = _check_account(db, settings, run_id, trigger, acct, prov, extractor, sleep)
                summary.results.append(result)
                if result.status in ("login_required", "blocked") or (
                    result.status == "failed" and result.error and result.error.startswith("[stop]")
                ):
                    stop_reason = f"collection stopped after @{acct['username']}: {result.error}"
        finally:
            prov.close()

    statuses = {r.status for r in summary.results}
    if stop_reason:
        summary.status = "stopped"
        summary.message = stop_reason
    elif statuses <= {"ok"}:
        summary.status = "ok"
    elif "ok" in statuses:
        summary.status = "partial"
    else:
        summary.status = "failed"
    if not summary.message:
        summary.message = (
            f"{summary.new_count} new item(s); "
            f"{sum(r.status == 'ok' for r in summary.results)}/{len(summary.results)} account(s) checked successfully."
        )
    log.info("Collection run %s: %s", run_id, summary.message)
    return summary


def _check_account(db, settings, run_id, trigger, acct, prov: Provider, extractor, sleep) -> AccountResult:
    username = acct["username"]
    is_initial = not acct["initial_import_done"]
    with db.tx() as conn:
        check_id = conn.execute(
            "INSERT INTO check_runs (run_id, account_id, trigger, provider, started_at, status, is_initial) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (run_id, acct["id"], trigger, prov.name, iso(utcnow()), int(is_initial)),
        ).lastrowid

    attempts = 0
    while True:
        attempts += 1
        try:
            posts = prov.fetch_recent(username, settings.max_posts_per_check)
            break
        except ProviderError as e:
            if e.retryable and attempts < settings.retry_attempts:
                delay = settings.retry_base_seconds * (2 ** (attempts - 1))
                log.info("@%s: transient error (%s); retrying in %.0fs", username, e, delay)
                sleep(delay)
                continue
            return _finish_failure(db, settings, check_id, acct, prov.last_used, e, attempts)
        except Exception as e:  # unexpected parser/driver error: record, don't crash the run
            log.exception("@%s: unexpected collection error", username)
            err = ProviderError(f"Unexpected error: {type(e).__name__}: {e}")
            return _finish_failure(db, settings, check_id, acct, prov.last_used, err, attempts)

    provider_name = prov.last_used
    fetched = len(posts)
    posts = sorted(posts, key=lambda p: p.published_at.timestamp() if p.published_at else 0, reverse=True)
    notes: list[str] = []
    if is_initial:
        posts = posts[: settings.initial_import_limit]
    else:
        posts = posts[: settings.max_posts_per_check]
        cutoff = parse_iso(acct["initial_import_at"])
        if cutoff:
            # Unseen posts older than the first check (e.g. newly pinned old posts) are outside
            # the tracking window; already-stored posts are left to the dedupe below.
            with db.read() as conn:
                known = {r[0] for r in conn.execute(
                    "SELECT post_key FROM items WHERE account_id = ?", (acct["id"],)
                )}
            old_unseen = [p for p in posts if p.key not in known and p.published_at and p.published_at < cutoff]
            if old_unseen:
                posts = [p for p in posts if p not in old_unseen]
                notes.append(f"ignored {len(old_unseen)} unseen post(s) published before tracking began")

    inserted: list[tuple[int, RawPost]] = []
    now_s = iso(utcnow())
    with db.tx() as conn:
        for p in posts:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO items (account_id, post_key, provider_post_id, shortcode, provider,
                    is_fixture, url, media_type, is_reel, published_at, caption, collected_at,
                    check_run_id, is_initial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acct["id"], p.key, p.post_id, p.shortcode, provider_name, int(prov.is_fixture), p.url,
                 p.media_type, int(p.is_reel), iso(p.published_at), p.caption, now_s, check_id,
                 int(is_initial)),
            )
            if cur.rowcount == 1:
                inserted.append((cur.lastrowid, p))

    if not is_initial and inserted and len(inserted) >= settings.max_posts_per_check:
        notes.append("every fetched post was new; older new posts beyond the per-check limit may be missing")

    for item_id, p in inserted:
        result = extractor.extract(p, prov)
        with db.tx() as conn:
            conn.execute(
                "UPDATE items SET extraction_status = ?, ocr_text = ?, transcript = ?, extraction_notes = ? WHERE id = ?",
                (result.status, result.ocr_text, result.transcript, "\n".join(result.notes) or None, item_id),
            )

    finished = iso(utcnow())
    with db.tx() as conn:
        conn.execute(
            "UPDATE check_runs SET status = 'ok', finished_at = ?, fetched_count = ?, new_count = ?, "
            "attempts = ?, provider = ?, notes = ? WHERE id = ?",
            (finished, fetched, len(inserted), attempts, provider_name, "\n".join(notes) or None, check_id),
        )
        conn.execute(
            "UPDATE accounts SET status = 'ok', last_checked_at = ?, last_success_at = ?, last_error = NULL, "
            "consecutive_failures = 0, next_attempt_at = NULL, initial_import_done = 1, "
            "initial_import_at = COALESCE(initial_import_at, ?) WHERE id = ?",
            (finished, finished, finished, acct["id"]),
        )
    return AccountResult(username, "ok", provider_name, len(inserted), fetched, is_initial, attempts, notes=notes)


def _finish_failure(db, settings, check_id, acct, provider_name, err: ProviderError, attempts) -> AccountResult:
    msg = str(err)
    if err.stops_run and err.status == "failed":
        msg = f"[stop] {msg}"
    now = utcnow()
    failures = acct["consecutive_failures"] + 1
    backoff = min(
        settings.failure_backoff_base_minutes * (2 ** (failures - 1)),
        settings.failure_backoff_max_hours * 60,
    )
    with db.tx() as conn:
        conn.execute(
            "UPDATE check_runs SET status = ?, finished_at = ?, attempts = ?, error = ?, provider = ? WHERE id = ?",
            (err.status, iso(now), attempts, msg, provider_name, check_id),
        )
        conn.execute(
            "UPDATE accounts SET status = ?, last_checked_at = ?, last_error = ?, consecutive_failures = ?, "
            "next_attempt_at = ? WHERE id = ?",
            (err.status, iso(now), msg, failures, iso(now + timedelta(minutes=backoff)), acct["id"]),
        )
    log.warning("@%s: check failed (%s): %s", acct["username"], err.status, msg)
    return AccountResult(acct["username"], err.status, provider_name, attempts=attempts, error=msg,
                         is_initial=not acct["initial_import_done"])


def _record_failure(db, settings, run_id, trigger, acct, provider_name, err, attempts) -> AccountResult:
    with db.tx() as conn:
        check_id = conn.execute(
            "INSERT INTO check_runs (run_id, account_id, trigger, provider, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (run_id, acct["id"], trigger, provider_name, iso(utcnow())),
        ).lastrowid
    return _finish_failure(db, settings, check_id, acct, provider_name, err, attempts)


def _record_skipped(db, run_id, trigger, acct, provider_name, reason) -> AccountResult:
    now = iso(utcnow())
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO check_runs (run_id, account_id, trigger, provider, started_at, finished_at, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, 'skipped', ?)",
            (run_id, acct["id"], trigger, provider_name, now, now, f"Not checked: {reason}"),
        )
    return AccountResult(acct["username"], "skipped", provider_name, error=f"Not checked: {reason}")
