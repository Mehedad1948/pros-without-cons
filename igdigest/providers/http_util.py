"""Bounded media download over HTTP."""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path

import httpx

from .base import ProviderError, TransientError


def download_bounded(client: httpx.Client, url: str, dest_dir: Path, max_bytes: int) -> Path:
    try:
        with client.stream("GET", url, follow_redirects=True) as resp:
            if resp.status_code >= 500 or resp.status_code == 429:
                raise TransientError(f"Media download failed with HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise ProviderError(f"Media download failed with HTTP {resp.status_code}")
            declared = int(resp.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise ProviderError(f"Media is larger than the {max_bytes} byte limit")
            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            ext = mimetypes.guess_extension(ctype) or ".bin"
            path = dest_dir / f"{uuid.uuid4().hex}{ext}"
            size = 0
            with path.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ProviderError(f"Media is larger than the {max_bytes} byte limit")
                    fh.write(chunk)
            return path
    except httpx.TransportError as e:
        raise TransientError(f"Network error while downloading media: {type(e).__name__}") from e
