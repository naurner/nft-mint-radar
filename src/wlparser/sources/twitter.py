"""Источник сигналов из Twitter/X.

Блокчейн подтверждает то, что уже случилось; твиттер говорит о том, что
только собирается случиться. Раздачу WL анонсируют здесь за часы до того,
как в сети появится первый минт, — ради этого источник и нужен.

Работает через twscrape: скрапинг под личным аккаунтом, только чтение.
Никаких публикаций, лайков и подписок; интервалы с разбросом, чтобы
поведение не выглядело машинным.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ..models import Signal
from .extract import classify, extract_addresses, extract_marketplace_slugs

log = logging.getLogger(__name__)


class TwitterSource:
    name = "twitter"

    def __init__(
        self,
        api,
        *,
        queries: list[str],
        watchlist: list[str] | None = None,
        chain_name: str = "robinhood",
        search_interval_s: float = 240.0,
        watchlist_interval_s: float = 75.0,
        jitter: float = 0.25,
        per_query_limit: int = 20,
        cursor_get=None,
        cursor_set=None,
    ):
        self._api = api
        self._queries = list(queries)
        self._watchlist = list(watchlist or [])
        self._chain = chain_name
        self._search_interval = search_interval_s
        self._watchlist_interval = watchlist_interval_s
        self._jitter = jitter
        self._limit = per_query_limit
        self._cursor_get = cursor_get
        self._cursor_set = cursor_set
        self._seen_cursor: dict[str, int] = {}

    # --- циклы ---

    async def run(self, out: "asyncio.Queue[Signal]") -> None:
        """Поиск и watchlist крутятся параллельно и независимо."""
        tasks = [self._loop_searches(out)]
        if self._watchlist:
            tasks.append(self._loop_watchlist(out))
        await asyncio.gather(*tasks)

    async def _loop_searches(self, out) -> None:
        while True:
            for query in self._queries:
                await self._guarded(self._run_search, query, out)
                await self._sleep(self._search_interval / max(len(self._queries), 1))
            if not self._queries:
                await self._sleep(self._search_interval)

    async def _loop_watchlist(self, out) -> None:
        while True:
            for handle in list(self._watchlist):
                await self._guarded(self._run_timeline, handle, out)
                await self._sleep(
                    self._watchlist_interval / max(len(self._watchlist), 1)
                )

    async def _guarded(self, fn, *args) -> None:
        """Сбой одного запроса не должен останавливать источник целиком."""
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("twitter: запрос не удался (%s)", args[0])
            await self._sleep(30)

    async def _sleep(self, seconds: float) -> None:
        spread = seconds * self._jitter
        await asyncio.sleep(max(seconds + random.uniform(-spread, spread), 1.0))

    # --- сбор ---

    async def _run_search(self, query: str, out) -> None:
        cursor_key = f"twitter:search:{query}"
        newest = await self._load_cursor(cursor_key)
        top = newest

        async for tweet in self._api.search(query, limit=self._limit):
            top = max(top, int(tweet.id))
            if int(tweet.id) <= newest:
                continue
            signal = self._to_signal(tweet, origin=f"search:{query}")
            if signal is not None:
                await out.put(signal)

        await self._save_cursor(cursor_key, top)

    async def _run_timeline(self, handle: str, out) -> None:
        user = await self._api.user_by_login(handle)
        if user is None:
            log.warning("twitter: аккаунт %s не найден", handle)
            return

        cursor_key = f"twitter:user:{user.id}"
        newest = await self._load_cursor(cursor_key)
        top = newest

        async for tweet in self._api.user_tweets(user.id, limit=self._limit):
            top = max(top, int(tweet.id))
            if int(tweet.id) <= newest:
                continue
            signal = self._to_signal(tweet, origin=f"watchlist:{handle}")
            if signal is not None:
                await out.put(signal)

        await self._save_cursor(cursor_key, top)

    # --- преобразование ---

    def _to_signal(self, tweet, *, origin: str) -> Signal | None:
        # Ретвит — это чужой текст, который мы и так увидим у автора.
        if getattr(tweet, "retweetedTweet", None) is not None:
            return None

        text = tweet.rawContent or ""
        kind = classify(text)
        addresses = extract_addresses(text)
        slugs = extract_marketplace_slugs(text)

        # Пустой трёп без единой зацепки хранить незачем: ни адреса,
        # ни ссылки на коллекцию — привязать такой сигнал не к чему.
        if kind == "chatter" and not addresses and not slugs:
            return None

        author = getattr(getattr(tweet, "user", None), "username", None)
        timestamp = getattr(tweet, "date", None) or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        return Signal(
            source="twitter",
            kind=kind,
            text=text,
            url=getattr(tweet, "url", None),
            author=author,
            contract_address=addresses[0] if addresses else None,
            chain=self._chain if addresses else None,
            timestamp=timestamp,
            raw={
                "tweet_id": str(tweet.id),
                "origin": origin,
                "addresses": addresses,
                "slugs": slugs,
                "likes": getattr(tweet, "likeCount", None),
                "author_followers": getattr(
                    getattr(tweet, "user", None), "followersCount", None
                ),
            },
            # Каждый твит учитывается отдельно, даже если он про уже
            # известный контракт: нарастающее обсуждение — это и есть сигнал.
            dedupe_override=f"twitter:tweet:{tweet.id}",
        )

    # --- курсоры ---

    async def _load_cursor(self, key: str) -> int:
        if key in self._seen_cursor:
            return self._seen_cursor[key]
        value = 0
        if self._cursor_get is not None:
            stored = await self._cursor_get(key)
            value = int(stored) if stored else 0
        self._seen_cursor[key] = value
        return value

    async def _save_cursor(self, key: str, value: int) -> None:
        if value <= self._seen_cursor.get(key, 0):
            return
        self._seen_cursor[key] = value
        if self._cursor_set is not None:
            await self._cursor_set(key, str(value))
