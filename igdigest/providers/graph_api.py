"""Official API provider: Instagram API with Facebook Login, Business Discovery.

What it can do (per Meta's docs, checked 2026-10-15):
  * Read public metadata and media of other Instagram *Business or Creator* accounts,
    queried through your own Instagram professional account's IG user ID.
  * Requires a Facebook User access token with instagram_basic, instagram_manage_insights
    and pages_read_engagement (plus ads_management/ads_read if the Page role comes from
    Business Manager).
What it cannot do:
  * Read personal (non-professional) accounts or age-gated accounts.
  * The Instagram Basic Display API (personal-account tokens) was shut down on 2024-12-04.
  * media_url is omitted for media containing copyrighted material (e.g. licensed reel audio).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

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
from .http_util import download_bounded

GRAPH_BASE = "https://graph.facebook.com"
_MEDIA_FIELDS = (
    "id,shortcode,permalink,caption,media_type,media_product_type,media_url,thumbnail_url,"
    "timestamp,children{media_type,media_url,thumbnail_url}"
)
_RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}


class GraphAPIProvider(Provider):
    name = "graph_api"

    def __init__(self, access_token: str | None, ig_user_id: str | None, api_version: str,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        if not access_token or not ig_user_id:
            raise ProviderConfigError(
                "Graph API provider needs IGD_GRAPH_ACCESS_TOKEN and IGD_GRAPH_IG_USER_ID."
            )
        self._token = access_token
        self._user_id = ig_user_id
        self._version = api_version
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def fetch_recent(self, username: str, limit: int) -> list[RawPost]:
        self.last_profile = None
        fields = (f"business_discovery.username({username}){{username,name,profile_picture_url,"
                  f"media.limit({int(limit)}){{{_MEDIA_FIELDS}}}}}")
        try:
            resp = self._client.get(
                f"{GRAPH_BASE}/{self._version}/{self._user_id}",
                params={"fields": fields},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.TransportError as e:
            raise TransientError(f"Network error contacting the Graph API: {type(e).__name__}") from e

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or "error" in body:
            raise _map_error(resp.status_code, body.get("error") or {})

        discovery = body.get("business_discovery") or {}
        self.last_profile = {"full_name": discovery.get("name"), "profile_pic_url": discovery.get("profile_picture_url")}
        media = discovery.get("media", {}).get("data", [])
        return [_to_post(m) for m in media][:limit]

    def download(self, media: MediaRef, dest_dir: Path, max_bytes: int) -> Path:
        if not media.url:
            raise ProviderError(media.note or "No media URL")
        return download_bounded(self._client, media.url, dest_dir, max_bytes)


def _map_error(status: int, err: dict) -> ProviderError:
    code = err.get("code")
    msg = err.get("message") or f"HTTP {status}"
    # Never include request URLs or tokens in messages.
    text = f"Graph API error {code}: {msg}"
    if code == 190 or status == 401:
        return LoginRequired(f"Graph API access token is invalid or expired. {text}")
    if code in _RATE_LIMIT_CODES or status == 429:
        return CollectionBlocked(f"Graph API rate limit reached; stopping. {text}")
    if code in (10, 200) or (isinstance(code, int) and 200 <= code < 300):
        return ProviderConfigError(f"Missing Graph API permission. {text}")
    if code in (100, 110) or status == 404:
        return AccountNotAccessible(
            "Business Discovery could not read this account. It only works for Business or "
            f"Creator accounts that are not age-gated. {text}"
        )
    if code in (1, 2) or status >= 500 or err.get("is_transient"):
        return TransientError(text)
    return ProviderError(text)


def _to_post(m: dict) -> RawPost:
    mtype = (m.get("media_type") or "IMAGE").upper()
    is_reel = (m.get("media_product_type") or "").upper() == "REELS"
    media: list[MediaRef] = []
    sources = [c for c in (m.get("children") or {}).get("data", [])] if mtype == "CAROUSEL_ALBUM" else [m]
    for s in sources:
        kind = "video" if (s.get("media_type") or "").upper() == "VIDEO" else "image"
        url = s.get("media_url")
        note = None if url else "Instagram withheld media_url (often copyrighted content such as licensed audio)"
        media.append(MediaRef(kind=kind, url=url, note=note))
    children = (m.get("children") or {}).get("data") or []
    first = children[0] if children else m
    thumbnail = first.get("thumbnail_url") or (first.get("media_url") if (first.get("media_type") or "").upper() != "VIDEO" else None)
    ts = m.get("timestamp")
    published = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") if ts else None
    shortcode = m.get("shortcode")
    return RawPost(
        post_id=str(m["id"]),
        shortcode=shortcode,
        url=m.get("permalink") or (f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""),
        media_type={"CAROUSEL_ALBUM": "carousel", "VIDEO": "video"}.get(mtype, "image"),
        is_reel=is_reel,
        published_at=published,
        caption=m.get("caption"),
        media=media,
        thumbnail_url=m.get("thumbnail_url") or thumbnail,
    )
