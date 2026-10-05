import asyncio
from datetime import datetime, timezone

from wlparser.sources.twitter import TwitterSource

ADDR = "0xb8a33b6e631773f962fe6ceea5db7746063670c9"


class FakeUser:
    def __init__(self, username="alpha", uid=777, followers=1200):
        self.id = uid
        self.username = username
        self.followersCount = followers


class FakeTweet:
    def __init__(
        self,
        tweet_id,
        content,
        *,
        user=None,
        retweeted=None,
        date=None,
        likes=5,
    ):
        self.id = tweet_id
        self.rawContent = content
        self.user = user or FakeUser()
        self.retweetedTweet = retweeted
        self.date = date or datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        self.url = f"https://x.com/alpha/status/{tweet_id}"
        self.likeCount = likes


class FakeApi:
    def __init__(self, search_results=None, timelines=None, users=None):
        self.search_results = search_results or {}
        self.timelines = timelines or {}
        self.users = users or {}
        self.searched = []

    async def search(self, q, limit=-1):
        self.searched.append(q)
        for tweet in self.search_results.get(q, []):
            yield tweet

    async def user_by_login(self, login):
        return self.users.get(login)

    async def user_tweets(self, uid, limit=-1):
        for tweet in self.timelines.get(uid, []):
            yield tweet


def make_source(api, **kwargs):
    kwargs.setdefault("queries", ["wl robinhood"])
    return TwitterSource(api, **kwargs)


async def collect(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# --- поиск ---


async def test_announcement_tweet_becomes_signal():
    api = FakeApi({"wl robinhood": [FakeTweet(100, "Mint is live now on Robinhood")]})
    queue = asyncio.Queue()
    await make_source(api)._run_search("wl robinhood", queue)
    [sig] = await collect(queue)
    assert sig.source == "twitter"
    assert sig.kind == "wl_announced"
    assert sig.author == "alpha"
    assert sig.url.endswith("/100")


async def test_contract_address_is_extracted_into_signal():
    api = FakeApi({"q": [FakeTweet(101, f"WL open for {ADDR}")]})
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    [sig] = await collect(queue)
    assert sig.contract_address == ADDR
    assert sig.chain == "robinhood"


async def test_pure_chatter_without_anchors_is_dropped():
    # Не к чему привязать: ни адреса, ни ссылки на коллекцию.
    api = FakeApi({"q": [FakeTweet(102, "gm, love this space")]})
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    assert queue.empty()


async def test_chatter_with_contract_is_kept_for_correlation():
    api = FakeApi({"q": [FakeTweet(103, f"anyone knows {ADDR}?")]})
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    [sig] = await collect(queue)
    assert sig.kind == "chatter"
    assert sig.contract_address == ADDR


async def test_retweets_are_skipped():
    # Оригинал мы и так увидим у автора; ретвит только удвоил бы ленту.
    original = FakeTweet(1, "Mint is live")
    api = FakeApi({"q": [FakeTweet(104, "Mint is live", retweeted=original)]})
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    assert queue.empty()


async def test_engagement_bait_is_downgraded_to_chatter():
    api = FakeApi({"q": [FakeTweet(105, f"Mint live! RT + follow {ADDR}")]})
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    [sig] = await collect(queue)
    assert sig.kind == "chatter"


async def test_each_tweet_has_its_own_dedupe_key():
    # Два твита про один контракт — два сигнала, а не один.
    api = FakeApi(
        {"q": [FakeTweet(106, f"WL open {ADDR}"), FakeTweet(107, f"mint live {ADDR}")]}
    )
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    signals = await collect(queue)
    assert len({s.dedupe_key for s in signals}) == 2


# --- курсоры ---


async def test_cursor_prevents_reprocessing_old_tweets():
    api = FakeApi({"q": [FakeTweet(200, "Mint is live"), FakeTweet(201, "WL open")]})
    stored = {}

    async def get(key):
        return stored.get(key)

    async def put(key, value):
        stored[key] = value

    source = make_source(api, queries=["q"], cursor_get=get, cursor_set=put)
    queue = asyncio.Queue()
    await source._run_search("q", queue)
    assert len(await collect(queue)) == 2
    assert stored["twitter:search:q"] == "201"

    await source._run_search("q", queue)
    assert queue.empty()


async def test_cursor_is_loaded_from_storage_on_start():
    api = FakeApi({"q": [FakeTweet(200, "Mint is live"), FakeTweet(205, "WL open")]})

    async def get(key):
        return "200"

    async def put(key, value):
        pass

    source = make_source(api, queries=["q"], cursor_get=get, cursor_set=put)
    queue = asyncio.Queue()
    await source._run_search("q", queue)
    signals = await collect(queue)
    assert [s.raw["tweet_id"] for s in signals] == ["205"]


# --- watchlist ---


async def test_watchlist_timeline_is_read():
    user = FakeUser(username="caller", uid=42)
    api = FakeApi(
        timelines={42: [FakeTweet(300, "Mint is live", user=user)]},
        users={"caller": user},
    )
    queue = asyncio.Queue()
    source = make_source(api, watchlist=["caller"])
    await source._run_timeline("caller", queue)
    [sig] = await collect(queue)
    assert sig.author == "caller"
    assert sig.raw["origin"] == "watchlist:caller"


async def test_unknown_watchlist_account_is_skipped_quietly():
    api = FakeApi(users={})
    queue = asyncio.Queue()
    await make_source(api, watchlist=["ghost"])._run_timeline("ghost", queue)
    assert queue.empty()


# --- устойчивость ---


async def test_failing_query_does_not_stop_the_source():
    class Broken(FakeApi):
        async def search(self, q, limit=-1):
            raise RuntimeError("rate limited")
            yield  # pragma: no cover

    source = make_source(Broken(), queries=["q"], jitter=0)
    source._sleep = lambda s: asyncio.sleep(0)
    queue = asyncio.Queue()
    await source._guarded(source._run_search, "q", queue)  # не должно бросить
    assert queue.empty()


async def test_naive_timestamp_is_treated_as_utc():
    tweet = FakeTweet(400, "Mint is live", date=datetime(2026, 9, 8, 12, 0))
    api = FakeApi({"q": [tweet]})
    queue = asyncio.Queue()
    await make_source(api, queries=["q"])._run_search("q", queue)
    [sig] = await collect(queue)
    assert sig.timestamp.tzinfo is timezone.utc
