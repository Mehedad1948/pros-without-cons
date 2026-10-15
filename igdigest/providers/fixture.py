"""Fixture provider: serves sample posts from a JSON file so the pipeline runs offline.

It never pretends to be Instagram: items are flagged is_fixture, links point to example.com,
and unknown usernames fail instead of getting sample content.

File format:
{
  "accounts": {
    "<username>": {
      "simulate": null | "login_required" | "blocked" | "transient" | "not_accessible",
      "posts": [
        {"id": "...", "shortcode": "...", "media_type": "image|video|carousel", "is_reel": false,
         "published_at": "2026-10-14T08:00:00Z", "caption": "...",
         "media": [{"kind": "image", "text": "simulated OCR text"}, {"kind": "video"}]}
      ]
    }
  }
}
"""

from __future__ import annotations

import json
from pathlib import Path

from ..db import parse_iso
from .base import (
    AccountNotAccessible,
    CollectionBlocked,
    LoginRequired,
    MediaRef,
    Provider,
    ProviderConfigError,
    ProviderError,
    RawPost,
    TransientError,
)

_SIMULATED = {
    "login_required": (LoginRequired, "Simulated: Instagram login required (fixture)."),
    "blocked": (CollectionBlocked, "Simulated: Instagram presented a challenge (fixture)."),
    "transient": (TransientError, "Simulated: temporary server error (fixture)."),
    "not_accessible": (AccountNotAccessible, "Simulated: account not accessible (fixture)."),
}


class FixtureProvider(Provider):
    name = "fixture"
    is_fixture = True

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise ProviderConfigError(f"Fixture file not found: {self.path}") from e
        except json.JSONDecodeError as e:
            raise ProviderConfigError(f"Fixture file is not valid JSON: {e}") from e

    def fetch_recent(self, username: str, limit: int) -> list[RawPost]:
        accounts = self._load().get("accounts", {})
        entry = accounts.get(username.lower())
        if entry is None:
            raise AccountNotAccessible(
                f"@{username} is not in the fixture file; fixture mode has no data for it."
            )
        simulate = entry.get("simulate")
        if simulate:
            cls, msg = _SIMULATED.get(simulate, (ProviderError, f"Simulated failure: {simulate}"))
            raise cls(msg)

        posts = []
        for p in entry.get("posts", []):
            shortcode = p.get("shortcode") or p["id"]
            media = [
                MediaRef(
                    kind=m.get("kind", "image"),
                    url=None,
                    fixture_text=m.get("text"),
                    note=None if m.get("text") is not None else "fixture media has no extractable content",
                )
                for m in p.get("media", [])
            ]
            posts.append(
                RawPost(
                    post_id=str(p["id"]),
                    shortcode=shortcode,
                    url=f"https://example.com/igdigest-fixture/{username}/{shortcode}",
                    media_type=p.get("media_type", "image"),
                    is_reel=bool(p.get("is_reel")),
                    published_at=parse_iso(p.get("published_at")),
                    caption=p.get("caption"),
                    media=media,
                )
            )
        posts.sort(key=lambda x: x.published_at.timestamp() if x.published_at else 0, reverse=True)
        return posts[:limit]

    def download(self, media: MediaRef, dest_dir: Path, max_bytes: int) -> Path:
        raise ProviderError("Fixture media cannot be downloaded.")
