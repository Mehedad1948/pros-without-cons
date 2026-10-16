"""Telegram HTML rendering. All third-party text is escaped; links come from stored post URLs."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..db import parse_iso
from ..reporting import excerpt
from .api import MAX_MESSAGE_CHARS

LIMIT = MAX_MESSAGE_CHARS - 200  # headroom for entities and the part counter
CAPTION_LIMIT = 1000  # Telegram photo captions: 1024 characters after entity parsing
SEPARATOR = "━" * 14

STATUS_ICON = {
    "ok": "✅", "new": "\U0001f195", "failed": "❌", "login_required": "\U0001f510",
    "blocked": "⛔", "not_accessible": "\U0001f6ab", "skipped": "⏭",
}
TYPE_LABEL = {
    "reel": "\U0001f3ac Reel", "video": "\U0001f39e Video", "carousel": "\U0001f5c2 Carousel",
    "image": "\U0001f5bc Post", "post": "\U0001f5bc Post",
}


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def account_icon(row) -> str:
    return "⏸" if row["paused"] else STATUS_ICON.get(row["status"], "•")


def pack(header: str, blocks: list[str]) -> list[str]:
    """Split into messages under Telegram's length limit, never inside a block."""
    flat: list[str] = []
    for block in blocks:
        if len(block) <= LIMIT:
            flat.append(block)
            continue
        # Each line is a complete HTML element, so splitting on lines keeps tags balanced.
        for line in block.split("\n"):
            flat.append(line if len(line) <= LIMIT else esc(html.unescape(_strip_tags(line))[: LIMIT - 1]) + "…")
    messages, current = [], header
    for block in flat:
        if len(current) + len(block) + 2 > LIMIT:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    if len(messages) > 1:
        messages = [f"{m}\n\n<i>({i}/{len(messages)})</i>" for i, m in enumerate(messages, start=1)]
    return messages


def _strip_tags(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


def _checks_block(cov: dict) -> str | None:
    lines = []
    for f in cov.get("failed", []):
        if not f.get("latest_failed", True):
            continue
        icon = STATUS_ICON.get(f["status"], "⚠️")
        lines.append(f"{icon} <b>@{esc(f['username'])}</b>: {esc(f['status_text'])} — {esc(f.get('error') or '')}")
    for u in cov.get("unchecked", []):
        lines.append(f"• @{esc(u)}: not checked yet")
    if not lines:
        return None
    return "⚠️ <b>Check problems</b>\n" + "\n".join(lines)


def ago(value: str | None, now: datetime, tz: ZoneInfo) -> str:
    """'just now', '4 min ago', '2 h ago', 'yesterday', '3 days ago', then a date."""
    dt = parse_iso(value)
    if dt is None:
        return "time unknown"
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    local = dt.astimezone(tz)
    return local.strftime("%d %b") if local.year == now.astimezone(tz).year else local.strftime("%d %b %Y")


def visible_len(text: str) -> int:
    """Length Telegram counts for captions: text after HTML entities are parsed."""
    return len(html.unescape(_strip_tags(text)))


def _post_block(p: dict, caption_chars: int, now: datetime, tz: ZoneInfo) -> str:
    when = ago(p.get("published_at"), now, tz) if p.get("published_at") else esc(p.get("published") or "time unknown")
    lines = [f"{TYPE_LABEL.get(p['type'], p['type'])} · <i>{when}</i>"]
    caption = excerpt(p.get("caption"), caption_chars)
    lines.append(f"<blockquote expandable>{esc(caption)}</blockquote>" if caption else "<i>(no caption)</i>")
    lines.append(f"\U0001f517 {link(p['url'], 'Open on Instagram')}")
    return "\n".join(lines)


def _account_messages(a: dict, fixture: bool, now: datetime, tz: ZoneInfo) -> list[dict]:
    name = a.get("full_name") or a["username"]
    count = len(a["posts"])
    header = (f"\U0001f4e3 <b>{esc(name)}</b>\n"
              f"@{esc(a['username'])} · \U0001f195 {count} new post{'s' if count != 1 else ''}")
    if a.get("initial"):
        header += " · <i>initial recap</i>"
    if fixture:
        header += "\n\U0001f9ea <i>sample data, not real Instagram content</i>"
    header += f"\n{SEPARATOR}"
    photo = next((p["thumbnail_url"] for p in a["posts"] if p.get("thumbnail_url")), None) or a.get("profile_pic_url")

    if photo:
        # One photo message per account: shorten caption excerpts until everything fits.
        for chars in (300, 200, 120, 70):
            caption = "\n\n".join([header] + [_post_block(p, chars, now, tz) for p in a["posts"]])
            if visible_len(caption) <= CAPTION_LIMIT:
                return [{"text": caption, "photo": photo}]
        # Too many posts for one caption: as many as fit under the photo, the rest as text.
        caption, rest = header, []
        for p in a["posts"]:
            block = _post_block(p, 120, now, tz)
            if not rest and visible_len(f"{caption}\n\n{block}") <= CAPTION_LIMIT:
                caption = f"{caption}\n\n{block}"
            else:
                rest.append(_post_block(p, 300, now, tz))
        more = pack(f"\U0001f4e3 <b>{esc(name)}</b> · continued", rest)
        return [{"text": caption, "photo": photo}] + [{"text": t, "photo": None} for t in more]

    return [{"text": t, "photo": None} for t in pack(header, [_post_block(p, 300, now, tz) for p in a["posts"]])]


def updates_messages(c: dict, now: datetime | None = None) -> list[dict]:
    """One message per account (photo + bold name + posts), then one message for check problems."""
    tz = ZoneInfo(c.get("timezone") or "UTC")
    now = now or datetime.now(timezone.utc)
    messages: list[dict] = []
    for a in c["accounts"]:
        messages += _account_messages(a, bool(c.get("is_fixture")), now, tz)
    problems = []
    if c.get("status_message"):
        problems.append(esc(c["status_message"]))
    checks = _checks_block(c.get("coverage", {}))
    if checks:
        problems.append(checks)
    problems += [f"ℹ️ {esc(n)}" for n in c.get("notes", [])]
    if problems:
        messages += [{"text": t, "photo": None} for t in pack("", problems)]
    return messages


def digest_messages(c: dict) -> list[dict]:
    header = "\U0001f4f0 <b>Instagram digest</b>"
    if c.get("is_fixture"):
        header += "\n\U0001f9ea <i>Fixture mode: sample data</i>"
    header += f"\n<i>{esc(c['window_text'])} · {esc(c['summarizer'])}</i>"
    if c.get("status_message"):
        header += f"\n\n{esc(c['status_message'])}"
    if c.get("overview"):
        header += f"\n\n{esc(c['overview'])}"

    def refs(sources):
        return " ".join(link(s["url"], s["ref"]) for s in sources)

    blocks = []
    for a in c["accounts"]:
        lines = [f"\U0001f464 <b>@{esc(a['username'])}</b>" + (" · <i>initial recap</i>" if a.get("initial") else "")]
        if a["claims"]:
            lines.append("<i>What the creator says</i>")
            lines += [f"• {esc(p['text'])} {refs(p['sources'])}" for p in a["claims"]]
        if a["interpretations"]:
            lines.append("<i>Interpretation (the app's analysis)</i>")
            lines += [f"• {esc(p['text'])} {refs(p['sources'])}" for p in a["interpretations"]]
        if a["other_posts"]:
            lines.append(f"Other new posts: {refs(a['other_posts'])}")
        blocks.append("\n".join(lines))
    if c.get("themes"):
        blocks.append("\U0001f501 <b>Recurring themes</b> <i>(interpretation)</i>\n" +
                      "\n".join(f"• {esc(t['text'])} {refs(t['sources'])}" for t in c["themes"]))
    checks = _checks_block(c.get("coverage", {}))
    if checks:
        blocks.append(checks)
    partial = c.get("coverage", {}).get("partial_media", [])
    if partial:
        blocks.append(f"ℹ️ {len(partial)} post(s) were only partly processed (e.g. caption only).")
    return [{"text": t, "photo": None} for t in pack(header, blocks)]


def failure_signature(c: dict) -> str:
    """Identifies the set of currently failing accounts, to avoid repeating identical alerts."""
    failed = sorted(f"{f['username']}:{f['status']}" for f in c.get("coverage", {}).get("failed", [])
                    if f.get("latest_failed", True))
    return ",".join(failed)
