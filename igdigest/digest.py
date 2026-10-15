"""Feature 2 - digest: an LLM-written recap of new posts. Requires an LLM provider.

Independent of updates reports: it has its own delivery tracking (digest_items) and is simply
unavailable when no LLM is configured. A scheduled digest takes every post not yet part of a
successful scheduled digest; previews never mark posts as delivered; if the LLM call fails the
digest is stored as failed and its posts roll into the next one.
"""

from __future__ import annotations

import json
import logging

from .config import Settings
from .db import Database, iso, utcnow
from .llm import DigestDraft, LLMError, Summarizer, build_summarizer
from .reporting import (
    coverage,
    coverage_markdown,
    fmt_local,
    last_window_end,
    status_message,
    undelivered_items,
    window_text,
)
from .repo import acquire_lock, release_lock

log = logging.getLogger(__name__)

TABLE = "digests"
UNAVAILABLE_HINT = (
    "The digest (LLM recap) needs an LLM: set IGD_LLM_PROVIDER (and its key). "
    "For a plain list of new posts without an LLM, use `igdigest updates`."
)


class DigestBusy(Exception):
    pass


class DigestUnavailable(Exception):
    pass


def build_digest(
    db: Database,
    settings: Settings,
    *,
    kind: str = "scheduled",
    trigger: str = "manual",
    summarizer: Summarizer | None = None,
) -> int:
    """Create a digest and return its id. kind = 'scheduled' (marks delivered) or 'preview'."""
    if kind not in ("scheduled", "preview"):
        raise ValueError(kind)
    if summarizer is None:
        if not settings.digest_available:
            raise DigestUnavailable(UNAVAILABLE_HINT)
        try:
            summarizer = build_summarizer(settings)
        except LLMError as e:
            raise DigestUnavailable(f"LLM is not usable: {e}") from e
    owner = None
    if kind == "scheduled":
        owner = acquire_lock(db, "digest", 10)
        if not owner:
            raise DigestBusy("Another scheduled digest is being generated.")
    try:
        return _build(db, settings, kind, trigger, summarizer)
    finally:
        if owner:
            release_lock(db, "digest", owner)


def _build(db, settings, kind, trigger, summarizer: Summarizer) -> int:
    now = iso(utcnow())
    with db.read() as conn:
        window_start = last_window_end(conn, TABLE)
        all_items = undelivered_items(conn, TABLE)
        cov = coverage(conn, window_start, now)

    items = all_items[: settings.max_items_per_digest]
    is_fixture = settings.is_fixture_mode or any(i["is_fixture"] for i in items)
    wtext = window_text(window_start, now, settings)
    notes: list[str] = []
    if len(all_items) > len(items):
        notes.append(f"{len(all_items) - len(items)} more new item(s) exceeded the per-digest limit and will appear in the next digest.")

    content: dict = {}
    status, error = "ok", None
    try:
        posts, ref_map = _payload(items)
        draft = summarizer.summarize(posts, wtext, settings.digest_language) if posts else None
        content = _assemble(
            items=items, ref_map=ref_map, draft=draft, cov=cov, kind=kind, trigger=trigger,
            is_fixture=is_fixture, window_start=window_start, window_end=now, window_text=wtext,
            settings=settings, summarizer_label=summarizer.label, notes=notes,
        )
    except LLMError as e:
        log.warning("Digest LLM call failed: %s", e)
        status, error = "failed", f"LLM summarization failed: {e}"
    except Exception as e:  # record the failure; items stay undelivered
        log.exception("Digest generation failed")
        status, error = "failed", f"{type(e).__name__}: {e}"

    md = render_markdown(content) if content else None
    with db.tx() as conn:
        digest_id = conn.execute(
            """
            INSERT INTO digests (kind, trigger, status, created_at, window_start, window_end, language,
                summarizer, item_count, is_fixture, content_json, content_md, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, trigger, status, now, window_start, now, settings.digest_language,
             summarizer.label, len(items) if status == "ok" else 0, int(is_fixture),
             json.dumps(content, ensure_ascii=False) if content else None, md, error),
        ).lastrowid
        if status == "ok" and kind == "scheduled":
            conn.executemany(
                "INSERT INTO digest_items (digest_id, item_id) VALUES (?, ?)",
                [(digest_id, i["id"]) for i in items],
            )
    return digest_id


def _payload(items) -> tuple[list[dict], dict[str, dict]]:
    posts, ref_map = [], {}
    for n, it in enumerate(items, start=1):
        ref = f"P{n}"
        ref_map[ref] = dict(it)
        posts.append({
            "ref": ref,
            "account": it["username"],
            "published_at": it["published_at"],
            "type": "reel" if it["is_reel"] else it["media_type"],
            "caption": it["caption"],
            "ocr_text": it["ocr_text"],
            "transcript": it["transcript"],
            "coverage": "full" if it["extraction_status"] == "full" else "partial",
        })
    return posts, ref_map


def _assemble(*, items, ref_map, draft: DigestDraft | None, cov, kind, trigger, is_fixture, window_start,
              window_end, window_text, settings, summarizer_label, notes) -> dict:
    def src(ref: str) -> dict:
        it = ref_map[ref]
        return {"ref": ref, "url": it["url"], "published": fmt_local(it["published_at"], settings.tz)}

    accounts: dict[str, dict] = {}
    for it in items:
        acc = accounts.setdefault(it["username"], {
            "username": it["username"], "claims": [], "interpretations": [], "other_posts": [],
            "item_count": 0, "initial": True,
        })
        acc["item_count"] += 1
        acc["initial"] = acc["initial"] and bool(it["is_initial"])

    dropped = 0
    cited: set[str] = set()
    themes = []
    overview = None
    if draft is not None:
        overview = draft.overview.strip()
        for p in draft.points:
            refs = [r for r in dict.fromkeys(p.sources) if r in ref_map]
            if not refs or not p.text.strip():
                dropped += 1
                continue
            owner = ref_map[refs[0]]["username"]
            refs = [r for r in refs if ref_map[r]["username"] == owner]
            cited.update(refs)
            entry = {"text": p.text.strip(), "sources": [src(r) for r in refs]}
            accounts[owner]["claims" if p.kind == "claim" else "interpretations"].append(entry)
        for t in draft.themes:
            refs = [r for r in dict.fromkeys(t.sources) if r in ref_map]
            if not refs or not t.text.strip():
                dropped += 1
                continue
            themes.append({"text": t.text.strip(), "sources": [src(r) for r in refs]})
    if dropped:
        notes.append(f"{dropped} summarizer point(s) without a valid source post were removed.")

    for ref, it in ref_map.items():
        if ref not in cited:
            accounts[it["username"]]["other_posts"].append(src(ref))

    partial_media = [
        {
            "username": it["username"], "ref": ref, "url": it["url"],
            "status": it["extraction_status"],
            "notes": [n for n in (it["extraction_notes"] or "").split("\n") if n],
        }
        for ref, it in ref_map.items()
        if it["extraction_status"] != "full"
    ]

    return {
        "kind": kind,
        "trigger": trigger,
        "is_fixture": is_fixture,
        "generated_at": window_end,
        "generated_local": fmt_local(window_end, settings.tz),
        "window_start": window_start,
        "window_end": window_end,
        "window_text": window_text,
        "timezone": settings.timezone,
        "language": settings.digest_language,
        "summarizer": summarizer_label,
        "item_count": len(items),
        "overview": overview,
        "status_message": status_message(len(items), cov),
        "accounts": sorted(accounts.values(), key=lambda a: a["username"]),
        "themes": themes,
        "coverage": {**cov, "partial_media": partial_media},
        "notes": notes,
    }


def _links(sources: list[dict]) -> str:
    return " ".join(f"[{s['ref']}]({s['url']})" for s in sources)


def render_markdown(c: dict) -> str:
    kind = "Scheduled digest" if c["kind"] == "scheduled" else "Preview (does not mark posts as delivered)"
    out = [f"# Instagram digest — {kind}", ""]
    if c["is_fixture"]:
        out += ["> **FIXTURE MODE** — sample data from the fixture file, not real Instagram content.", ""]
    out += [
        f"- Window: {c['window_text']}",
        f"- Generated: {c['generated_local']} ({c['timezone']})",
        f"- Language: {c['language']} · Summarizer: {c['summarizer']}",
        f"- New items: {c['item_count']}",
        "",
    ]
    if c["status_message"]:
        out += [f"**{c['status_message']}**", ""]
    if c["overview"]:
        out += ["## Overview", "", c["overview"], ""]
    if c["accounts"]:
        out += ["## Key points by account", ""]
        for a in c["accounts"]:
            label = " — initial recap (latest posts available at first check)" if a["initial"] else ""
            out += [f"### @{a['username']}{label}", ""]
            if a["claims"]:
                out.append("**What the creator says**")
                out += [f"- {p['text']} {_links(p['sources'])}" for p in a["claims"]]
                out.append("")
            if a["interpretations"]:
                out.append("**Interpretation (app's analysis, not the creator's words)**")
                out += [f"- {p['text']} {_links(p['sources'])}" for p in a["interpretations"]]
                out.append("")
            if a["other_posts"]:
                out += [f"Other new posts: {_links(a['other_posts'])}", ""]
    if c["themes"]:
        out += ["## Recurring themes (interpretation)", ""]
        out += [f"- {t['text']} {_links(t['sources'])}" for t in c["themes"]]
        out.append("")
    out += ["## Coverage", ""]
    out += coverage_markdown(c["coverage"], c["coverage"]["partial_media"], c["notes"])
    out.append("")
    return "\n".join(out)
