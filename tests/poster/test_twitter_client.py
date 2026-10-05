"""Патч на x_client_transaction (см. wlparser/poster/twitter_client.py).

X сменила формат чанков webpack на своей странице в апреле 2026, и
регулярка твикит 2.3.3 для поиска файла ondemand.s.js перестала совпадать
— это и есть баг "Couldn't get KEY_BYTE indices" из github.com/d60/twikit/
issues/408. Тесты гоняют настоящий twikit.x_client_transaction.transaction
(зависимость проекта), а не имитацию — сам regex-парсинг и есть суть фикса.
"""

import bs4

from wlparser.poster.twitter_client import (
    TwikitPoster,
    _patch_transaction_indices,
    _patch_user_fields,
)

OLD_FORMAT_HTML = """
<html><body>
<script>{"ondemand.s":"abc123def"}</script>
</body></html>
"""

NEW_FORMAT_HTML = """
<html><body>
<script>
var nameMap = {20113:"ondemand.s", 99:"other.chunk"};
var hashMap = {20113:"2c5bb94", 99:"deadbeef"};
</script>
</body></html>
"""

LEGACY_COMMA_FORMAT_HTML = """
<html><body>
<script>
var nameMap = {99:'other.chunk',20113:'ondemand.s'};
var hashMap = {99:"deadbeef",20113:"2c5bb94"};
</script>
</body></html>
"""

NO_MATCH_HTML = "<html><body><script>nothing here</script></body></html>"

INDICES_JS = """
var a = (x[3],16); var b = (y[7],16); var c = (z[19],16);
"""


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeSession:
    def __init__(self, response_text):
        self._response_text = response_text
        self.requested_urls = []

    async def request(self, *, method, url, headers):
        self.requested_urls.append(url)
        return FakeResponse(self._response_text)


class FakeClientTransaction:
    """Минимум, что нужно патченному get_indices: validate_response + атрибут."""

    def __init__(self, html):
        self.home_page_response = bs4.BeautifulSoup(html, "html.parser")

    def validate_response(self, response):
        return response if isinstance(response, bs4.BeautifulSoup) else None


def _get_indices():
    _patch_transaction_indices()
    from twikit.x_client_transaction import transaction as tx

    return tx.ClientTransaction.get_indices


async def test_patch_is_idempotent():
    _patch_transaction_indices()
    _patch_transaction_indices()  # второй вызов не должен упасть/задвоиться


async def test_old_format_resolves_the_hash_and_fetches_the_right_url():
    get_indices = _get_indices()
    ct = FakeClientTransaction(OLD_FORMAT_HTML)
    session = FakeSession(INDICES_JS)

    row_index, key_indices = await get_indices(ct, ct.home_page_response, session, {})

    assert row_index == 3
    assert key_indices == [7, 19]
    assert session.requested_urls == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.abc123defa.js"
    ]


async def test_new_webpack_chunk_format_resolves_via_the_hash_map():
    get_indices = _get_indices()
    ct = FakeClientTransaction(NEW_FORMAT_HTML)
    session = FakeSession(INDICES_JS)

    row_index, key_indices = await get_indices(ct, ct.home_page_response, session, {})

    assert row_index == 3
    assert key_indices == [7, 19]
    assert session.requested_urls == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.2c5bb94a.js"
    ]


async def test_legacy_comma_format_is_tried_as_a_second_fallback():
    # Отличается от NEW_FORMAT только кавычками вокруг 'ondemand.s' — этого
    # достаточно, чтобы паттерн из PR #416 не совпал, и должен сработать
    # третий, независимо предложенный в том же issue вариант (с запятой
    # перед chunk id и хешем только из hex-цифр).
    get_indices = _get_indices()
    ct = FakeClientTransaction(LEGACY_COMMA_FORMAT_HTML)
    session = FakeSession(INDICES_JS)

    row_index, key_indices = await get_indices(ct, ct.home_page_response, session, {})

    assert row_index == 3
    assert key_indices == [7, 19]
    assert session.requested_urls == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.2c5bb94a.js"
    ]


async def test_no_match_still_raises_the_original_error():
    get_indices = _get_indices()
    ct = FakeClientTransaction(NO_MATCH_HTML)
    session = FakeSession(INDICES_JS)

    try:
        await get_indices(ct, ct.home_page_response, session, {})
    except Exception as exc:
        assert "Couldn't get KEY_BYTE indices" in str(exc)
    else:
        raise AssertionError("expected an exception")


# --- User.__init__ ("KeyError: 'urls'" — twikit/user.py) ---


def _minimal_user_data(**legacy_overrides) -> dict:
    legacy = {
        "created_at": "Mon Jan 01 00:00:00 +0000 2020",
        "name": "Bot",
        "screen_name": "bot",
        "profile_image_url_https": "https://example.com/a.jpg",
        "location": "",
        "description": "",
        "entities": {"description": {}},
        "verified": False,
        "possibly_sensitive": False,
        "can_dm": True,
        "can_media_tag": False,
        "want_retweets": False,
        "default_profile": True,
        "default_profile_image": False,
        "has_custom_timelines": False,
        "followers_count": 0,
        "fast_followers_count": 0,
        "normal_followers_count": 0,
        "friends_count": 0,
        "favourites_count": 0,
        "listed_count": 0,
        "media_count": 0,
        "statuses_count": 0,
        "is_translator": False,
        "translator_type": "none",
    }
    legacy.update(legacy_overrides)
    return {"rest_id": "123", "is_blue_verified": False, "legacy": legacy}


def _make_user(data: dict):
    _patch_user_fields()
    from twikit.user import User

    return User(object(), data)


def test_patch_user_fields_is_idempotent():
    _patch_user_fields()
    _patch_user_fields()


def test_missing_description_urls_defaults_to_empty_list():
    user = _make_user(_minimal_user_data())
    assert user.description_urls == []


def test_missing_pinned_tweet_ids_defaults_to_empty_list():
    user = _make_user(_minimal_user_data())
    assert user.pinned_tweet_ids == []


def test_missing_withheld_in_countries_defaults_to_empty_list():
    user = _make_user(_minimal_user_data())
    assert user.withheld_in_countries == []


def test_present_optional_fields_pass_through_unchanged():
    data = _minimal_user_data(
        entities={"description": {"urls": ["http://example.com"]}},
        pinned_tweet_ids_str=["555"],
        withheld_in_countries=["DE"],
    )
    user = _make_user(data)
    assert user.description_urls == ["http://example.com"]
    assert user.pinned_tweet_ids == ["555"]
    assert user.withheld_in_countries == ["DE"]


def test_required_fields_still_populate_normally():
    user = _make_user(_minimal_user_data())
    assert user.id == "123"
    assert user.screen_name == "bot"


# --- TwikitPoster.post / quote ---


class FakeTweet:
    def __init__(self, id, screen_name):
        self.id = id
        self.user = type("U", (), {"screen_name": screen_name})() if screen_name else None


class FakeClient:
    def __init__(self):
        self.calls = []

    async def create_tweet(self, **kwargs):
        self.calls.append(kwargs)
        return FakeTweet("123", "bot")


async def test_post_sends_plain_text_and_builds_the_url():
    poster = TwikitPoster({"auth_token": "x", "ct0": "y"})
    poster._client = FakeClient()

    url = await poster.post("hello")

    assert url == "https://x.com/bot/status/123"
    assert poster._client.calls == [{"text": "hello"}]


async def test_quote_attaches_the_quoted_tweet_url():
    poster = TwikitPoster({"auth_token": "x", "ct0": "y"})
    poster._client = FakeClient()

    url = await poster.quote("nice find", "https://x.com/a/status/1")

    assert url == "https://x.com/bot/status/123"
    assert poster._client.calls == [
        {"text": "nice find", "attachment_url": "https://x.com/a/status/1"}
    ]


def test_tweet_url_is_none_without_an_id_or_username():
    assert TwikitPoster._tweet_url(FakeTweet(None, "bot")) is None
    assert TwikitPoster._tweet_url(FakeTweet("123", None)) is None
