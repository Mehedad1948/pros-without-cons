"""Browser connector: page classification and timeline parsing (no browser launched)."""

from pathlib import Path

import pytest

from igdigest.providers.base import CollectionBlocked, LoginRequired
from igdigest.providers.browser import CONSENT_MESSAGE, BrowserProvider, parse_timeline


@pytest.fixture
def provider():
    return BrowserProvider(Path("unused.json"))


def test_cookie_consent_redirect_is_reported_as_action_needed(provider):
    with pytest.raises(CollectionBlocked) as exc:
        provider._check_page_state(
            "https://www.instagram.com/consent/?flow=user_cookie_choice_v2&source=pft_user_cookie_choice", 200)
    assert str(exc.value) == CONSENT_MESSAGE and "igdigest login" in str(exc.value)


def test_login_redirect_and_http_429(provider):
    with pytest.raises(LoginRequired):
        provider._check_page_state("https://www.instagram.com/accounts/login/?next=/nasa/", 200)
    with pytest.raises(CollectionBlocked, match="429"):
        provider._check_page_state("https://www.instagram.com/nasa/", 429)


def node(code, media_type, product_type, taken_at, **extra):
    return {"node": {"code": code, "pk": f"pk-{code}", "media_type": media_type, "product_type": product_type,
                     "taken_at": taken_at, "caption": {"text": f"caption {code}"},
                     "image_versions2": {"candidates": [{"url": f"https://cdn.example/{code}.jpg"}]}, **extra}}


def test_parse_profile_timeline_response():
    data = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        node("REEL1", 2, "clips", 1791645302, video_versions=[{"url": "https://cdn.example/r.mp4"}]),
        node("CAR1", 8, "carousel_container", 1790974365, caption=None, carousel_media=[
            {"media_type": 1, "image_versions2": {"candidates": [{"url": "https://cdn.example/a.jpg"}]}},
            {"media_type": 2, "video_versions": []},
        ]),
        node("IMG1", 1, "feed", 1792079344),
        {"node": {"pk": "no-code"}},
    ]}}}
    posts = {p.shortcode: p for p in parse_timeline(data)}
    assert set(posts) == {"REEL1", "CAR1", "IMG1"}
    reel = posts["REEL1"]
    assert reel.is_reel and reel.url == "https://www.instagram.com/reel/REEL1/" and reel.media[0].kind == "video"
    car = posts["CAR1"]
    assert car.media_type == "carousel" and car.caption is None
    assert [m.kind for m in car.media] == ["image", "video"] and car.media[1].url is None and car.media[1].note
    assert posts["IMG1"].caption == "caption IMG1" and posts["IMG1"].published_at.year == 2026
