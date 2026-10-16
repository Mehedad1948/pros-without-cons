"""Provider interface: how posts are collected from Instagram (or a fixture)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class MediaRef:
    kind: str  # "image" | "video"
    url: str | None = None
    note: str | None = None  # why the media is unavailable, if it is
    # Fixture provider only: simulated extracted text, labelled as fixture output.
    fixture_text: str | None = None


@dataclass
class RawPost:
    post_id: str
    url: str
    shortcode: str | None = None
    media_type: str = "image"  # image | video | carousel
    is_reel: bool = False
    published_at: datetime | None = None
    caption: str | None = None
    media: list[MediaRef] = field(default_factory=list)
    thumbnail_url: str | None = None  # cover image (also for reels/carousels), for display

    @property
    def key(self) -> str:
        """Stable dedupe key. Shortcodes are shared by the web UI and Graph API."""
        return self.shortcode or f"id:{self.post_id}"


class ProviderError(Exception):
    """Base error. `status` is stored on the account and the check run."""

    status = "failed"
    retryable = False
    stops_run = False  # True when continuing would just hit the same wall for every account


class TransientError(ProviderError):
    retryable = True


class LoginRequired(ProviderError):
    status = "login_required"
    stops_run = True


class CollectionBlocked(ProviderError):
    status = "blocked"
    stops_run = True


class AccountNotAccessible(ProviderError):
    status = "not_accessible"


class ProviderConfigError(ProviderError):
    status = "failed"
    stops_run = True


class Provider(ABC):
    name: str = "base"
    is_fixture: bool = False
    # Pause between accounts within one run (politeness / rate limits).
    pace_seconds: float = 0.0

    def __enter__(self) -> "Provider":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:  # noqa: B027 - optional hook
        pass

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    # Profile details seen during the last fetch_recent, if any: {"full_name", "profile_pic_url"}.
    last_profile: dict | None = None

    @property
    def last_used(self) -> str:
        """Name of the concrete provider that served the last fetch (composite providers override)."""
        return self.name

    @abstractmethod
    def fetch_recent(self, username: str, limit: int) -> list[RawPost]:
        """Return up to `limit` most recent posts (newest first if possible). Raise ProviderError."""

    @abstractmethod
    def download(self, media: MediaRef, dest_dir: Path, max_bytes: int) -> Path:
        """Download one media file into dest_dir and return its path. Raise ProviderError."""
