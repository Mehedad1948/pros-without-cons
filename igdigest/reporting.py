"""Helpers shared by both report types (updates and LLM digests). No LLM code here."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .db import parse_iso

FAILED_STATUSES = ("failed", "login_required", "blocked", "not_accessible", "skipped")
STATUS_TEXT = {
    "failed": "check failed",
    "login_required": "login required",
    "blocked": "collection blocked",
    "not_accessible": "account not accessible",
    "skipped": "not checked (run stopped early)",
}

# report table -> (link table, link column). Each feature tracks delivery separately.
_DELIVERY = {
    "update_reports": ("update_report_items", "report_id"),
    "digests": ("digest_items", "digest_id"),
}


def fmt_local(value: str | None, tz: ZoneInfo) -> str:
    dt: datetime | None = parse_iso(value)
    if dt is None:
        return "unknown time"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def last_window_end(conn: sqlite3.Connection, report_table: str) -> str | None:
    """End of the previous delivering ('scheduled') report of this type that succeeded."""
    row = conn.execute(
        f"SELECT window_end FROM {report_table} WHERE kind = 'scheduled' AND status = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["window_end"] if row else None


def undelivered_items(conn: sqlite3.Connection, report_table: str) -> list[sqlite3.Row]:
    """Items not yet part of a successful delivering report of this type."""
    link_table, link_col = _DELIVERY[report_table]
    return conn.execute(
        f"""
        SELECT i.*, a.username FROM items i JOIN accounts a ON a.id = i.account_id
        WHERE i.id NOT IN (
            SELECT l.item_id FROM {link_table} l JOIN {report_table} r ON r.id = l.{link_col}
            WHERE r.kind = 'scheduled' AND r.status = 'ok'
        )
        ORDER BY i.collected_at, i.published_at, i.id
        """
    ).fetchall()


def coverage(conn: sqlite3.Connection, window_start: str | None, window_end: str) -> dict:
    """Which accounts were checked successfully, failed, or were not checked in the window."""
    params: list = [window_end]
    where = "cr.started_at <= ?"
    if window_start:
        where += " AND cr.started_at >= ?"
        params.append(window_start)
    runs = conn.execute(
        f"SELECT cr.*, a.username FROM check_runs cr JOIN accounts a ON a.id = cr.account_id "
        f"WHERE {where} ORDER BY cr.started_at",
        params,
    ).fetchall()
    accounts = conn.execute("SELECT * FROM accounts ORDER BY username").fetchall()
    by_account: dict[int, list] = {}
    for r in runs:
        by_account.setdefault(r["account_id"], []).append(r)

    failed, unchecked, paused, ok = [], [], [], []
    for a in accounts:
        acc_runs = by_account.get(a["id"], [])
        if a["paused"] and not acc_runs:
            paused.append(a["username"])
            continue
        fails = [r for r in acc_runs if r["status"] in FAILED_STATUSES]
        successes = [r for r in acc_runs if r["status"] == "ok"]
        if fails:
            last_fail = fails[-1]
            failed.append({
                "username": a["username"],
                "status": last_fail["status"],
                "status_text": STATUS_TEXT.get(last_fail["status"], last_fail["status"]),
                "error": last_fail["error"],
                "had_success": bool(successes),
                "latest_failed": acc_runs[-1]["status"] in FAILED_STATUSES,
                "failed_checks": len(fails),
            })
        elif not acc_runs:
            unchecked.append(a["username"])
        if successes:
            ok.append(a["username"])
    return {"checks_run": len(runs), "ok_accounts": ok, "failed": failed, "unchecked": unchecked, "paused": paused}


def status_message(item_count: int, cov: dict) -> str | None:
    """Keeps 'no new posts' distinct from 'could not check'."""
    if item_count:
        return None
    if cov["checks_run"] == 0:
        return "No checks ran in this window, so there is nothing new to report."
    if cov["failed"] and not cov["ok_accounts"]:
        return ("No account could be checked successfully in this window. This is not the same "
                "as “no new posts”: new posts may exist but were not collected.")
    if cov["failed"]:
        return (f"No new posts from the accounts that were checked successfully. "
                f"{len(cov['failed'])} account(s) had failed checks, so posts there may have been missed.")
    return "No new posts in this window."


def window_text(window_start: str | None, window_end: str, settings: Settings) -> str:
    start = fmt_local(window_start, settings.tz) if window_start else "start of tracking"
    return f"{start} → {fmt_local(window_end, settings.tz)} ({settings.timezone})"


def coverage_markdown(cov: dict, partial_media: list[dict] | None = None, notes: list[str] | None = None) -> list[str]:
    """partial_media=None means the report does not cover media processing at all."""
    out: list[str] = []
    reports_media = partial_media is not None
    partial_media = partial_media or []
    if not (cov["failed"] or cov["unchecked"] or partial_media):
        out.append("- All monitored accounts were checked." + (" All media was processed." if reports_media else ""))
    for f in cov["failed"]:
        when = "latest check failed" if f["latest_failed"] else "an earlier check failed; a later one succeeded"
        out.append(f"- @{f['username']}: {f['status_text']} ({when}; {f['failed_checks']} failed check(s)) — {f['error']}")
    for u in cov["unchecked"]:
        out.append(f"- @{u}: not checked in this window")
    if cov["paused"]:
        out.append(f"- Paused (not checked): {', '.join('@' + u for u in cov['paused'])}")
    for m in partial_media:
        detail = "; ".join(m["notes"]) or m["status"]
        out.append(f"- Partial coverage [{m['ref']}]({m['url']}) (@{m['username']}, {m['status']}): {detail}")
    for n in notes or []:
        out.append(f"- Note: {n}")
    return out


def excerpt(text: str | None, limit: int = 300) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
