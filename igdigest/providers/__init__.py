"""Provider factory."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .base import (
    AccountNotAccessible,
    MediaRef,
    Provider,
    ProviderConfigError,
    ProviderError,
    RawPost,
)


class AutoProvider(Provider):
    """Official API first; browser connector only for accounts the API cannot read.

    Login/challenge/rate-limit errors from either side are raised unchanged, never retried
    on the other connector, and never replaced by fixture data.
    """

    name = "auto"

    def __init__(self, graph: Provider | None, browser: Provider | None):
        if graph is None and browser is None:
            raise ProviderConfigError(
                "auto provider needs Graph API credentials and/or a saved browser session."
            )
        self.graph = graph
        self.browser = browser
        self._last: Provider | None = None
        self.pace_seconds = browser.pace_seconds if browser else 0.0

    @property
    def last_used(self) -> str:
        return self._last.name if self._last else self.name

    def close(self) -> None:
        for p in (self.graph, self.browser):
            if p is not None:
                p.close()

    def fetch_recent(self, username: str, limit: int) -> list[RawPost]:
        if self.graph is not None:
            try:
                self._last = self.graph
                return self.graph.fetch_recent(username, limit)
            except AccountNotAccessible:
                if self.browser is None:
                    raise
        self._last = self.browser
        return self.browser.fetch_recent(username, limit)

    def download(self, media: MediaRef, dest_dir: Path, max_bytes: int) -> Path:
        if self._last is None:
            raise ProviderError("No provider has fetched yet")
        return self._last.download(media, dest_dir, max_bytes)


def build_provider(settings: Settings, name: str | None = None) -> Provider:
    name = name or settings.provider
    if name == "fixture":
        from .fixture import FixtureProvider

        return FixtureProvider(settings.fixture_path)
    if name == "graph_api":
        from .graph_api import GraphAPIProvider

        token = settings.graph_access_token.get_secret_value() if settings.graph_access_token else None
        return GraphAPIProvider(token, settings.graph_ig_user_id, settings.graph_api_version)
    if name == "browser":
        from .browser import BrowserProvider

        return BrowserProvider(
            settings.session_file,
            headless=settings.browser_headless,
            delay_seconds=settings.browser_delay_seconds,
            timeout_seconds=settings.browser_timeout_seconds,
            proxy=settings.browser_proxy,
        )
    if name == "auto":
        from ..session_store import session_exists

        graph = None
        if settings.graph_access_token and settings.graph_ig_user_id:
            graph = build_provider(settings, "graph_api")
        browser = build_provider(settings, "browser") if session_exists(settings.session_file) else None
        return AutoProvider(graph, browser)
    raise ProviderConfigError(f"Unknown provider: {name}")


__all__ = ["build_provider", "Provider", "RawPost", "MediaRef", "ProviderError"]
