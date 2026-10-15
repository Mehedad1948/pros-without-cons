import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from igdigest.config import Settings
from igdigest.db import init_db


def make_posts(prefix: str, n: int, start: datetime | None = None) -> list[dict]:
    start = start or datetime(2026, 10, 1, tzinfo=timezone.utc)
    return [
        {
            "id": f"{prefix}-{i}",
            "shortcode": f"{prefix.upper()}{i}",
            "media_type": "image",
            "published_at": (start + timedelta(hours=i)).isoformat(),
            "caption": f"{prefix} caption {i}",
            "media": [{"kind": "image", "text": f"{prefix} ocr {i}"}],
        }
        for i in range(1, n + 1)
    ]


class FixtureFile:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"accounts": {}}
        self.save()

    def set_account(self, username: str, posts=None, simulate=None):
        self.data["accounts"][username] = {"posts": posts or [], "simulate": simulate}
        self.save()

    def add_post(self, username: str, post: dict):
        self.data["accounts"][username]["posts"].append(post)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


@pytest.fixture
def fixture_file(tmp_path) -> FixtureFile:
    return FixtureFile(tmp_path / "fixture.json")


@pytest.fixture
def settings(tmp_path, fixture_file) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        provider="fixture",
        fixture_path=fixture_file.path,
        retry_base_seconds=5,
        scheduler_enabled=False,
        llm_provider="none",
    )


@pytest.fixture
def db(settings):
    return init_db(settings.database_file)


@pytest.fixture
def no_sleep():
    calls = []
    return calls, calls.append
