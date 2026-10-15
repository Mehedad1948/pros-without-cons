import httpx
import pytest
from fastapi.testclient import TestClient

from igdigest.providers.base import AccountNotAccessible, LoginRequired
from igdigest.providers.graph_api import GraphAPIProvider
from igdigest.repo import InvalidAccount, parse_username
from igdigest.web.app import create_app


@pytest.mark.parametrize("value,expected", [
    ("nasa", "nasa"),
    ("@NASA", "nasa"),
    ("https://www.instagram.com/nasa/", "nasa"),
    ("instagram.com/nasa?igsh=abc", "nasa"),
    ("http://instagram.com/some.user_1", "some.user_1"),
])
def test_parse_username(value, expected):
    assert parse_username(value) == expected


@pytest.mark.parametrize("value", [
    "https://www.instagram.com/p/C0abc/", "https://www.instagram.com/reel/C0abc/",
    "https://ig.me/j/AbCdEf", "https://example.com/nasa", "not valid!", "",
])
def test_parse_username_rejects_non_profiles(value):
    with pytest.raises(InvalidAccount):
        parse_username(value)


def test_web_add_account_preview_and_cross_origin_block(settings):
    app = create_app(settings, start_scheduler=False)
    with TestClient(app) as client:
        r = client.post("/accounts", data={"value": "instagram.com/alpha, @beta"})
        assert r.status_code == 200 and "@alpha" in r.text and "@beta" in r.text
        assert "FIXTURE MODE" in r.text

        r = client.post("/updates", data={"kind": "preview"})
        assert r.status_code == 200 and "No checks ran" in r.text

        # No LLM configured: the digest is off, updates keep working.
        r = client.post("/digests", data={"kind": "preview"})
        assert r.status_code == 200 and "needs an LLM" in r.text

        r = client.post("/accounts", data={"value": "evil"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403


def graph(handler):
    return GraphAPIProvider("token", "1784", "v25.0", transport=httpx.MockTransport(handler))


def test_graph_api_maps_expired_token_and_non_business_accounts():
    expired = graph(lambda req: httpx.Response(400, json={"error": {"code": 190, "message": "Session expired"}}))
    with pytest.raises(LoginRequired):
        expired.fetch_recent("alpha", 5)

    personal = graph(lambda req: httpx.Response(400, json={"error": {"code": 110, "message": "Cannot find User"}}))
    with pytest.raises(AccountNotAccessible):
        personal.fetch_recent("alpha", 5)


def test_graph_api_parses_media_and_keeps_token_out_of_url():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        return httpx.Response(200, json={"business_discovery": {"media": {"data": [
            {"id": "1", "shortcode": "R1", "permalink": "https://www.instagram.com/reel/R1/",
             "media_type": "VIDEO", "media_product_type": "REELS", "caption": "hi",
             "timestamp": "2026-10-14T10:00:00+0000"},
        ]}}})

    posts = graph(handler).fetch_recent("alpha", 5)
    assert posts[0].is_reel and posts[0].key == "R1"
    assert posts[0].media[0].url is None and "withheld" in posts[0].media[0].note
    assert "token" not in seen["url"] and seen["auth"] == "Bearer token"
