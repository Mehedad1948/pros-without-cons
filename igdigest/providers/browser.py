"""Best-effort local browser connector (Playwright) using your own saved Instagram session.

It opens each profile page like a normal visit and reads the post data that page loads by
itself (it makes no extra API calls), one profile at a time with a fixed pause between profiles. It does NOT bypass
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
# Data the profile page fetches for its post grid (instagram.com web client, observed 2026-10).
_TIMELINE_KEY = "xdt_api__v1__feed__user_timeline_graphql_connection"
_NOT_FOUND_TEXT = ("sorry, this page isn't available", "page not found")
_PRIVATE_TEXT = ("this account is private",)
_CHALLENGE_MARKERS = ("/challenge", "/checkpoint", "/accounts/suspended", "/auth_platform")
_CONSENT_MARKER = "instagram.com/consent"
CONSENT_MESSAGE = (
    "Instagram is asking this session to choose cookie settings (consent page). "
    "Run `igdigest login` and make the choice in the browser window."
)

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
        self.last_profile = None
        captured: list[dict] = []
        refused: list[int] = []

        def on_response(resp) -> None:
            if "instagram.com" not in resp.url or "graphql" not in resp.url:
                return
            if resp.status == 429:
                refused.append(resp.status)
                return
            if resp.status != 200:
                return
            try:
                text = resp.text()
            except Exception:
                return
            if _TIMELINE_KEY in text:
                try:
                    captured.append(json.loads(text))
                except ValueError:
                    pass

        page.on("response", on_response)
        try:
            try:
                resp = page.goto(f"{IG}/{username}/", wait_until="domcontentloaded")
            except Exception as e:
                raise TransientError(f"Profile page did not load: {type(e).__name__}") from e
            self._check_page_state(page.url, resp.status if resp else None)
            deadline = time.monotonic() + min(20.0, self.timeout_ms / 1000)
            while not captured and not refused and time.monotonic() < deadline:
                page.wait_for_timeout(500)
            self._check_page_state(page.url, None)  # late redirects to login/consent/challenge
        finally:
            page.remove_listener("response", on_response)

        if captured:
            self.last_profile = profile_from_timeline(captured[0], username)
            return parse_timeline(captured[0])[:limit]
        if refused:
            raise CollectionBlocked(
                "Instagram refused the profile's post data (HTTP 429): a rate limit, or a block on this "
                "session/network. Stopping; wait a few hours before checking again."
            )
        try:
            body = page.inner_text("body", timeout=5000)[:5000].lower()
        except Exception:
            body = ""
        if any(t in body for t in _NOT_FOUND_TEXT):
            raise AccountNotAccessible(f"@{username} was not found or is unavailable.")
        if any(t in body for t in _PRIVATE_TEXT):
            raise AccountNotAccessible(f"@{username} is private and your account does not follow it.")
        # The page loaded but its post data was not recognised (layout changed?): read the grid instead.
        return self._fallback_dom(username, limit)

    def _check_page_state(self, url: str, status: int | None) -> None:
        low = url.lower()
        if _CONSENT_MARKER in low:
            raise CollectionBlocked(CONSENT_MESSAGE)
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


def parse_timeline(data: dict) -> list[RawPost]:
    """Posts from the profile page's own timeline response (pinned posts included; caller sorts)."""
    connection = (data.get("data") or {}).get(_TIMELINE_KEY) or {}
    posts = [_timeline_node_to_post(e.get("node") or {}) for e in connection.get("edges") or []]
    return [p for p in posts if p is not None]


def profile_from_timeline(data: dict, username: str) -> dict | None:
    """Display name and picture of the profile owner (co-authored posts carry other users)."""
    connection = (data.get("data") or {}).get(_TIMELINE_KEY) or {}
    for edge in connection.get("edges") or []:
        user = (edge.get("node") or {}).get("user") or {}
        if (user.get("username") or "").lower() == username.lower():
            return {"full_name": user.get("full_name") or None, "profile_pic_url": user.get("profile_pic_url")}
    return None


def _cover_url(node: dict) -> str | None:
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    return candidates[0].get("url") if candidates else None


def _media_ref(node: dict) -> MediaRef:
    videos = node.get("video_versions") or []
    if node.get("media_type") == 2 or videos:
        url = videos[0].get("url") if videos else None
        return MediaRef(kind="video", url=url, note=None if url else "video URL not exposed by the page")
    url = _cover_url(node)
    return MediaRef(kind="image", url=url, note=None if url else "image URL not exposed by the page")


def _timeline_node_to_post(node: dict) -> RawPost | None:
    code = node.get("code")
    if not code:
        return None
    children = node.get("carousel_media") or []
    is_reel = node.get("product_type") == "clips"
    ts = node.get("taken_at")
    if children or node.get("media_type") == 8:
        media_type = "carousel"
    elif node.get("media_type") == 2:
        media_type = "video"
    else:
        media_type = "image"
    return RawPost(
        post_id=str(node.get("pk") or node.get("id") or code),
        shortcode=code,
        url=f"{IG}/{'reel' if is_reel else 'p'}/{code}/",
        media_type=media_type,
        is_reel=is_reel,
        published_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
        caption=(node.get("caption") or {}).get("text"),
        media=[_media_ref(c) for c in (children or [node])],
        thumbnail_url=_cover_url(node) or (_cover_url(children[0]) if children else None),
    )


def interactive_login(session_path: Path, proxy: str | None, timeout_minutes: float) -> bool:
    """Open a visible browser; the user signs in (incl. 2FA) themselves; save session state.

    Never reads, types, or stores the password. Only the resulting cookies/storage are saved.
    """
    from ..session_store import save_session_state

    sync_playwright = _playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options(False, proxy))
        # Reuse a saved session so an existing login only needs the remaining steps (e.g. consent).
        context = browser.new_context(storage_state=str(session_path) if session_exists(session_path) else None)
        page = context.new_page()
        deadline = time.monotonic() + timeout_minutes * 60

        def logged_in() -> bool:
            return any(c.get("name") == "sessionid" and c.get("value") for c in context.cookies(IG))

        def wait_until(done) -> bool:
            while time.monotonic() < deadline:
                try:
                    if done():
                        return True
                    page.wait_for_timeout(2000)
                except Exception:
                    return False  # browser closed by the user
            return False

        ok = False
        try:
            if not logged_in():
                page.goto(f"{IG}/accounts/login/")
                print("A browser window is open. Sign in to Instagram there and complete any 2FA.")
                print("Do not type your password here. Waiting for the login to finish...")
                ok = wait_until(logged_in)
            else:
                print("Using the saved session.")
                ok = True
            if ok:
                page.wait_for_timeout(2000)  # let post-login cookies settle
                page.goto(f"{IG}/instagram/", wait_until="domcontentloaded")
                if _CONSENT_MARKER in page.url.lower():
                    print("Instagram is asking for your cookie choice. Choose an option in the browser window.")
                    print("(If the page does not change afterwards, open any profile in that window.)")
                    ok = wait_until(lambda: _CONSENT_MARKER not in page.url.lower() and logged_in())
                    if ok:
                        print("Cookie choice saved.")
            if ok:
                save_session_state(session_path, context.storage_state())
        except Exception as e:
            print(f"Login window closed or failed: {type(e).__name__}")
            ok = False
        try:
            browser.close()
        except Exception:
            pass
    return ok
