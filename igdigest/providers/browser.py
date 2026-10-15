"""Best-effort local browser connector (Playwright) using your own saved Instagram session.

It reads what your logged-in account can normally see, the same way the Instagram web app
does, one profile at a time with a fixed pause between profiles. It does NOT bypass
CAPTCHAs or challenges, rotate proxies/accounts, or spoof fingerprints: when Instagram asks
for login or shows a challenge it raises LoginRequired / CollectionBlocked and the run stops.

Instagram's web endpoints are undocumented and change without notice; expect breakage.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..session_store import session_exists
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

IG = "https://www.instagram.com"
# Public app id that instagram.com's own web client sends with its API requests.
_WEB_APP_ID = "936619743392459"
_CHALLENGE_MARKERS = ("/challenge", "/checkpoint", "/accounts/suspended", "/auth_platform")

_PROFILE_JS = """
async (username) => {
  const r = await fetch('/api/v1/users/web_profile_info/?username=' + encodeURIComponent(username), {
    credentials: 'include',
    headers: {'x-ig-app-id': '%s', 'x-requested-with': 'XMLHttpRequest'}
  });
  return {status: r.status, url: r.url, text: await r.text()};
}
""" % _WEB_APP_ID


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ProviderConfigError(
            "Playwright is not installed. Run: pip install -e .[browser] && python -m playwright install chromium"
        ) from e
    return sync_playwright


def launch_options(headless: bool, proxy: str | None) -> dict:
    opts: dict = {"headless": headless}
    if proxy:
        opts["proxy"] = {"server": proxy}
    return opts


class BrowserProvider(Provider):
    name = "browser"

    def __init__(self, session_path: Path, headless: bool = True, delay_seconds: float = 8.0,
                 timeout_seconds: float = 45.0, proxy: str | None = None):
        self.session_path = Path(session_path)
        self.headless = headless
        self.pace_seconds = delay_seconds
        self.timeout_ms = int(timeout_seconds * 1000)
        self.proxy = proxy
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # --- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        if not session_exists(self.session_path):
            raise LoginRequired("No saved Instagram session. Run `igdigest login` first.")
        sync_playwright = _playwright()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(**launch_options(self.headless, self.proxy))
            self._context = self._browser.new_context(storage_state=str(self.session_path))
            self._context.set_default_timeout(self.timeout_ms)
            self._page = self._context.new_page()
        except Exception as e:
            self.close()
            raise ProviderError(f"Could not start the browser: {type(e).__name__}: {e}") from e

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None

    def _ensure_open(self):
        if self._page is None:
            self.open()
        return self._page

    # --- collection ------------------------------------------------------------
    def fetch_recent(self, username: str, limit: int) -> list[RawPost]:
        page = self._ensure_open()
        try:
            resp = page.goto(f"{IG}/{username}/", wait_until="domcontentloaded")
        except Exception as e:
            raise TransientError(f"Profile page did not load: {type(e).__name__}") from e
        self._check_page_state(page.url, resp.status if resp else None)

        try:
            result = page.evaluate(_PROFILE_JS, username)
        except Exception as e:
            raise TransientError(f"Profile request failed in the page: {type(e).__name__}") from e

        status, text = result.get("status"), result.get("text") or ""
        self._check_api_response(status, result.get("url") or "", text)
        try:
            data = json.loads(text)
        except ValueError:
            # Endpoint shape changed or returned HTML; fall back to reading the profile grid.
            return self._fallback_dom(username, limit)

        user = (data.get("data") or {}).get("user")
        if not user:
            raise AccountNotAccessible(f"@{username} was not found or is unavailable.")
        edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges", [])
        if user.get("is_private") and not user.get("followed_by_viewer") and not edges:
            raise AccountNotAccessible(f"@{username} is private and your account does not follow it.")
        posts = [_node_to_post(e.get("node") or {}) for e in edges]
        posts = [p for p in posts if p is not None]
        if not posts and (user.get("edge_owner_to_timeline_media") or {}).get("count"):
            return self._fallback_dom(username, limit)
        return posts[:limit]

    def _check_page_state(self, url: str, status: int | None) -> None:
        low = url.lower()
        if any(m in low for m in _CHALLENGE_MARKERS):
            raise CollectionBlocked("Instagram presented a challenge/checkpoint. Resolve it in a normal browser, then run `igdigest login`.")
        if "/accounts/login" in low:
            raise LoginRequired("Instagram session expired. Run `igdigest login`.")
        if status == 429:
            raise CollectionBlocked("Instagram rate-limited this session (HTTP 429). Stopping.")
        if status == 404:
            raise AccountNotAccessible("Profile not found (HTTP 404).")
        if status and status >= 500:
            raise TransientError(f"Instagram returned HTTP {status}.")

    def _check_api_response(self, status: int | None, url: str, text: str) -> None:
        low = text[:2000].lower()
        if "checkpoint_required" in low or "challenge_required" in low or any(m in url.lower() for m in _CHALLENGE_MARKERS):
            raise CollectionBlocked("Instagram requires a challenge/checkpoint. Resolve it manually, then run `igdigest login`.")
        if "login_required" in low or "/accounts/login" in url.lower() or status == 401:
            raise LoginRequired("Instagram session expired. Run `igdigest login`.")
        if status == 429 or "please wait a few minutes" in low:
            raise CollectionBlocked("Instagram rate-limited this session. Stopping.")
        if status == 404:
            raise AccountNotAccessible("Profile not found (HTTP 404).")
        if status and status >= 500:
            raise TransientError(f"Instagram returned HTTP {status}.")
        if status and status >= 400:
            raise ProviderError(f"Instagram returned HTTP {status}.")

    def _fallback_dom(self, username: str, limit: int) -> list[RawPost]:
        """Read post links from the profile grid and open each one (bounded) for its metadata."""
        page = self._page
        hrefs = page.eval_on_selector_all(
            "a[href*='/p/'], a[href*='/reel/']", "els => els.map(e => e.getAttribute('href'))"
        )
        shortcodes: list[tuple[str, bool]] = []
        for h in hrefs:
            m = re.search(r"/(p|reel)/([A-Za-z0-9_-]+)", h or "")
            if m and m.group(2) not in [s for s, _ in shortcodes]:
                shortcodes.append((m.group(2), m.group(1) == "reel"))
        posts = []
        for code, is_reel in shortcodes[:limit]:
            time.sleep(min(self.pace_seconds, 3))
            resp = page.goto(f"{IG}/{'reel' if is_reel else 'p'}/{code}/", wait_until="domcontentloaded")
            self._check_page_state(page.url, resp.status if resp else None)
            desc = page.get_attribute("meta[property='og:description']", "content") or ""
            dt = page.get_attribute("time[datetime]", "datetime")
            m = re.search(r':\s*"(.*)"\s*$', desc, re.S)
            posts.append(RawPost(
                post_id=code, shortcode=code, url=f"{IG}/{'reel' if is_reel else 'p'}/{code}/",
                media_type="video" if is_reel else "image", is_reel=is_reel,
                published_at=datetime.fromisoformat(dt.replace("Z", "+00:00")) if dt else None,
                caption=m.group(1) if m else None,
                media=[MediaRef(kind="video" if is_reel else "image", url=None,
                                note="media not exposed by the page fallback")],
            ))
        return posts

    def download(self, media: MediaRef, dest_dir: Path, max_bytes: int) -> Path:
        if not media.url:
            raise ProviderError(media.note or "No media URL")
        self._ensure_open()
        try:
            resp = self._context.request.get(media.url, timeout=self.timeout_ms)
        except Exception as e:
            raise TransientError(f"Media download failed: {type(e).__name__}") from e
        if resp.status >= 500 or resp.status == 429:
            raise TransientError(f"Media download failed with HTTP {resp.status}")
        if resp.status != 200:
            raise ProviderError(f"Media download failed with HTTP {resp.status}")
        if int(resp.headers.get("content-length") or 0) > max_bytes:
            raise ProviderError(f"Media is larger than the {max_bytes} byte limit")
        body = resp.body()
        if len(body) > max_bytes:
            raise ProviderError(f"Media is larger than the {max_bytes} byte limit")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip()
        path = dest_dir / f"{uuid.uuid4().hex}{mimetypes.guess_extension(ctype) or '.bin'}"
        path.write_bytes(body)
        return path


def _node_to_post(node: dict) -> RawPost | None:
    code = node.get("shortcode")
    if not code:
        return None
    typename = node.get("__typename", "")
    is_reel = node.get("product_type") == "clips"
    caption_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    caption = (caption_edges[0].get("node") or {}).get("text") if caption_edges else None
    children = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
    sources = [c.get("node") or {} for c in children] or [node]
    media = []
    for s in sources:
        if s.get("is_video"):
            url = s.get("video_url")
            media.append(MediaRef(kind="video", url=url, note=None if url else "video URL not exposed on the profile page"))
        else:
            media.append(MediaRef(kind="image", url=s.get("display_url")))
    ts = node.get("taken_at_timestamp")
    media_type = "carousel" if typename == "GraphSidecar" or children else ("video" if node.get("is_video") else "image")
    return RawPost(
        post_id=str(node.get("id") or code),
        shortcode=code,
        url=f"{IG}/{'reel' if is_reel else 'p'}/{code}/",
        media_type=media_type,
        is_reel=is_reel,
        published_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
        caption=caption,
        media=media,
    )


def interactive_login(session_path: Path, proxy: str | None, timeout_minutes: float) -> bool:
    """Open a visible browser; the user signs in (incl. 2FA) themselves; save session state.

    Never reads, types, or stores the password. Only the resulting cookies/storage are saved.
    """
    from ..session_store import save_session_state

    sync_playwright = _playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options(False, proxy))
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{IG}/accounts/login/")
        print("A browser window is open. Sign in to Instagram there and complete any 2FA.")
        print("Do not type your password here. Waiting for the login to finish...")
        deadline = time.monotonic() + timeout_minutes * 60
        logged_in = False
        while time.monotonic() < deadline:
            try:
                cookies = context.cookies(IG)
            except Exception:
                break  # browser closed by the user
            if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                logged_in = True
                break
            try:
                page.wait_for_timeout(2000)
            except Exception:
                break
        if logged_in:
            page.wait_for_timeout(2000)  # let post-login cookies settle
            save_session_state(session_path, context.storage_state())
        try:
            browser.close()
        except Exception:
            pass
    return logged_in
