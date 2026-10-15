"""Feature 1 - updates: a plain list of new posts per account, with links. No LLM involved.

Works on its own: it needs only collection. Delivery is tracked separately from LLM digests,
so a post appears once in updates and, independently, once in a digest (if digests are on).
"""

from __future__ import annotations

import json
import logging

from .config import Settings
from .db import Database, iso, utcnow
from .reporting import (
    coverage,
    coverage_markdown,
    excerpt,
    fmt_local,
    last_window_end,
    status_message,
    undelivered_items,
    window_text,
)
from .repo import acquire_lock, release_lock

log = logging.getLogger(__name__)

TABLE = "update_reports"


class UpdatesBusy(Exception):
    pass


def build_updates_report(
    db: Database,
    settings: Settings,
    *,
    kind: str = "scheduled",
    trigger: str = "manual",
    skip_if_empty: bool = False,
) -> int | None:
    """Create an updates report; kind='scheduled' marks its posts delivered, 'preview' does not.

    With skip_if_empty, nothing is stored when there are no new posts and no failed checks.
    """
    if kind not in ("scheduled", "preview"):
        raise ValueError(kind)
    owner = None
    if kind == "scheduled":
        owner = acquire_lock(db, "updates", 10)
        if not owner:
            raise UpdatesBusy("Another updates report is being generated.")
    try:
        return _build(db, settings, kind, trigger, skip_if_empty)
    finally:
        if owner:
            release_lock(db, "updates", owner)


def _build(db, settings, kind, trigger, skip_if_empty) -> int | None:
    now = iso(utcnow())
    with db.read() as conn:
        start = last_window_end(conn, TABLE)
        all_items = undelivered_items(conn, TABLE)
        cov = coverage(conn, start, now)
    if skip_if_empty and not all_items and not cov["failed"]:
        return None

    items = all_items[: settings.updates_max_items]
    notes = []
    if len(all_items) > len(items):
        notes.append(f"{len(all_items) - len(items)} more new post(s) will appear in the next report.")
    is_fixture = settings.is_fixture_mode or any(i["is_fixture"] for i in items)

    accounts: dict[str, dict] = {}
    for it in items:
        acc = accounts.setdefault(it["username"], {"username": it["username"], "initial": True, "posts": []})
        acc["initial"] = acc["initial"] and bool(it["is_initial"])
        acc["posts"].append({
            "url": it["url"],
            "published": fmt_local(it["published_at"], settings.tz) if it["published_at"] else None,
            "type": "reel" if it["is_reel"] else (it["media_type"] or "post"),
            "caption": excerpt(it["caption"]),
            "has_extracted_text": bool(it["ocr_text"] or it["transcript"]),
            "extraction_status": it["extraction_status"],
        })
    for acc in accounts.values():
        acc["posts"].sort(key=lambda p: p["published"] or "", reverse=True)

    content = {
        "kind": kind,
        "trigger": trigger,
        "is_fixture": is_fixture,
        "generated_local": fmt_local(now, settings.tz),
        "timezone": settings.timezone,
        "window_text": window_text(start, now, settings),
        "item_count": len(items),
        "status_message": status_message(len(items), cov),
        "accounts": sorted(accounts.values(), key=lambda a: a["username"]),
        "coverage": cov,
        "notes": notes,
    }
    md = render_markdown(content)
    with db.tx() as conn:
        report_id = conn.execute(
            """
            INSERT INTO update_reports (kind, trigger, status, created_at, window_start, window_end,
                item_count, is_fixture, content_json, content_md)
            VALUES (?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, trigger, now, start, now, len(items), int(is_fixture),
             json.dumps(content, ensure_ascii=False), md),
        ).lastrowid
        if kind == "scheduled":
            conn.executemany(
                "INSERT INTO update_report_items (report_id, item_id) VALUES (?, ?)",
                [(report_id, i["id"]) for i in items],
            )
    log.info("Updates report #%s: %s new post(s)", report_id, len(items))
    return report_id


def render_markdown(c: dict) -> str:
    kind = "Report" if c["kind"] == "scheduled" else "Preview (does not mark posts as delivered)"
    out = [f"# New Instagram posts — {kind}", ""]
    if c["is_fixture"]:
        out += ["> **FIXTURE MODE** — sample data from the fixture file, not real Instagram content.", ""]
    out += [f"- Window: {c['window_text']}", f"- New posts: {c['item_count']}", ""]
    if c["status_message"]:
        out += [f"**{c['status_message']}**", ""]
    for a in c["accounts"]:
        label = " — initial recap (latest posts available at first check)" if a["initial"] else ""
        out += [f"## @{a['username']}{label}", ""]
        for p in a["posts"]:
            when = p["published"] or "time unknown"
            caption = f" — “{p['caption']}”" if p["caption"] else " — (no caption)"
            out.append(f"- {when} · {p['type']}{caption} [open]({p['url']})")
        out.append("")
    out += ["## Checks", ""]
    out += coverage_markdown(c["coverage"], None, c["notes"])
    out.append("")
    return "\n".join(out)
