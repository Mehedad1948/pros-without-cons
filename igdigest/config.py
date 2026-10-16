"""Runtime configuration, read from environment variables (prefix IGD_) and .env."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["fixture", "graph_api", "browser", "auto"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IGD_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- storage -----------------------------------------------------------
    data_dir: Path = Path("data")
    db_path: Path | None = None  # default: <data_dir>/igdigest.db

    # --- collection --------------------------------------------------------
    provider: ProviderName = "fixture"
    fixture_path: Path = Path("fixtures/sample_posts.json")
    initial_import_limit: int = 5
    max_posts_per_check: int = 12
    max_media_per_post: int = 4
    max_media_bytes: int = 50_000_000
    retry_attempts: int = 3
    retry_base_seconds: float = 5.0
    failure_backoff_base_minutes: float = 30.0
    failure_backoff_max_hours: float = 24.0
    lock_ttl_minutes: float = 30.0

    # --- schedule ----------------------------------------------------------
    timezone: str = "Asia/Tehran"
    check_interval_hours: float = 6.0
    digest_time: str = "20:00"
    scheduler_enabled: bool = True
    collect_before_digest: bool = True

    # --- official API (Instagram API with Facebook Login, Business Discovery)
    graph_access_token: SecretStr | None = None
    graph_ig_user_id: str | None = None
    graph_api_version: str = "v25.0"

    # --- local authenticated browser connector -----------------------------
    session_path: Path | None = None  # default: <data_dir>/session/instagram_state.json
    browser_headless: bool = True
    browser_delay_seconds: float = 8.0
    browser_timeout_seconds: float = 45.0
    # Optional single fixed network proxy for Chromium (e.g. socks5://127.0.0.1:10808).
    # This is for local connectivity only; the app never rotates proxies.
    browser_proxy: str | None = None
    login_timeout_minutes: float = 10.0

    # --- media extraction --------------------------------------------------
    ocr_enabled: bool = False
    ocr_languages: str = "eng"
    tesseract_cmd: str | None = None
    transcribe_provider: Literal["none", "openai", "faster_whisper"] = "none"
    transcribe_model: str | None = None
    transcribe_api_key: SecretStr | None = None
    transcribe_base_url: str | None = None

    # --- feature 1: updates (new-post reports, no LLM) -----------------------
    updates_enabled: bool = True
    # After each scheduled check, write an updates report if there are new posts or failed checks.
    updates_after_check: bool = True
    updates_max_items: int = 100

    # --- feature 2: digest (LLM recap); active only when an LLM is configured
    digest_enabled: bool = True
    llm_provider: Literal["none", "anthropic", "openai"] = "none"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_max_tokens: int = 8000
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None  # Anthropic only
    llm_refusal_fallback: bool = True
    digest_language: str = "English"
    max_items_per_digest: int = 60

    # --- Telegram bot ------------------------------------------------------
    telegram_enabled: bool = False  # also start the bot inside `igdigest serve`
    telegram_bot_token: SecretStr | None = None
    # Comma-separated Telegram user IDs allowed to use the bot. Empty = nobody.
    telegram_allowed_user_ids: str = ""
    # Proxy for api.telegram.org, e.g. socks5h://127.0.0.1:10808 (HTTPS_PROXY is also honoured).
    telegram_proxy: str | None = None
    telegram_send_digests: bool = True
    telegram_poll_timeout: int = 25

    # --- web ---------------------------------------------------------------
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    @field_validator("digest_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        time.fromisoformat(v)
        return v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    # --- derived -----------------------------------------------------------
    @property
    def database_file(self) -> Path:
        return self.db_path or self.data_dir / "igdigest.db"

    @property
    def session_file(self) -> Path:
        return self.session_path or self.data_dir / "session" / "instagram_state.json"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def digest_hour_minute(self) -> tuple[int, int]:
        t = time.fromisoformat(self.digest_time)
        return t.hour, t.minute

    @property
    def is_fixture_mode(self) -> bool:
        return self.provider == "fixture"

    @property
    def telegram_allowed_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_user_ids.replace(";", ",").split(",") if x.strip().isdigit()}

    @property
    def digest_available(self) -> bool:
        """The LLM digest runs only when enabled and an LLM provider is configured."""
        return self.digest_enabled and self.llm_provider != "none"

    @property
    def resolved_llm_model(self) -> str | None:
        if self.llm_model:
            return self.llm_model
        if self.llm_provider == "anthropic":
            return "claude-opus-5-5"
        return None


def get_settings(**overrides) -> Settings:
    return Settings(**overrides)
