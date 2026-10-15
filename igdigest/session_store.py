"""Local storage of the browser session state with owner-only permissions."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path


def restrict_permissions(path: Path, is_dir: bool = False) -> None:
    """Best effort: owner-only access (chmod 600/700 on POSIX, icacls ACL on Windows)."""
    if sys.platform == "win32":
        user = os.environ.get("USERNAME") or getpass.getuser()
        grant = f"{user}:(OI)(CI)F" if is_dir else f"{user}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            check=False, capture_output=True,
        )
    else:
        os.chmod(path, 0o700 if is_dir else 0o600)


def save_session_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_permissions(path.parent, is_dir=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    restrict_permissions(tmp)
    os.replace(tmp, path)
    restrict_permissions(path)


def session_exists(path: Path) -> bool:
    return Path(path).is_file()
