"""Пост в X по каждой подходящей находке — публикуется сам, без

подтверждения владельцем: владелец явно попросил не дёргать его в
Telegram на каждую находку, на то это и автопостинг. `twitter` —
инжектируемый клиент с методами `async def post(text) -> str | None` и
`async def quote(text, tweet_url) -> str | None` (оба возвращают ссылку
на опубликованный твит).

Ручное подтверждение осталось только у send_test_draft/approve/skip —
они обслуживают явный, намеренный запрос владельца (/testpost), а не
фоновые находки, и это осознанное действие, а не уведомление о находке.

Счётного лимита на обычные посты больше нет (владелец попросил отбирать
по качеству, а не по числу) — вместо него caption_prompt/quote_prompt
сами решают, стоит ли вообще писать (см. caption.py::SKIP_TOKEN): это
ОДИН запрос к модели "решить и написать", не два подряд. Отдельная
проверка "стоит ли" перед запросом текста была здесь раньше и оказалась
физически неподъёмной для локальной модели на этом железе — почти
каждая находка требовала два запроса подряд, и оба обычно уходили в
тайм-аут, так что ничего не публиковалось вообще. При любой
неопределённости (модель выключена, недоступна, не ответила вовремя,
ответ не разобрать) решение всё равно — НЕ публиковать: пропустить
находку дешевле, чем опубликовать что-то посредственное, раз лимита по
количеству больше нет.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..enrich.models import marketplace_url
from ..models import Signal
from .caption import (
    body_budget,
    build_caption,
    caption_prompt,
    clean_generated_body,
    compose,
    is_skip_reply,
    quote_budget,
    quote_prompt,
)

# Таймаут одного запроса к модели — она на том же железе, что обслуживает
# ещё и фильтр качества алертов, тексты постов и дайджест, так что запас
# нужен щедрый, но не бесконечный: лучше пропустить находку, чем
# заблокировать на ней всю очередь сигналов.
_LLM_TIMEOUT_S = 25.0

log = logging.getLogger(__name__)

# Цитируем только явные анонсы, не любое упоминание темы — chatter слишком
# неопределённый (см. sources/extract.py::classify), комментировать там
# особо нечего.
QUOTABLE_KINDS = {"wl_announced"}


class Poster:
    def __init__(
        self,
        db,
        *,
        twitter,
        notify_owner,
        marketplace_chain: str = "robinhood",
        max_posts_per_day: int | None = None,
        max_quotes_per_day: int | None = 5,
        llm=None,
    ):
        self._db = db
        self._twitter = twitter
        # Отправляет черновик владельцу на подтверждение: async fn(post_id, text).
        self._notify_owner = notify_owner
        self._chain = marketplace_chain
        self._max_per_day = max_posts_per_day
        self._max_quotes_per_day = max_quotes_per_day
        # Локальная модель (llm.py::LocalLLM) — необязательна: без неё
        # собственный текст поста остаётся шаблонным, а пороги "стоит ли"
        # просто не применяются (см. docstring модуля). Цитирование без
        # модели не работает вовсе — без неё нечем написать комментарий.
        self._llm = llm

    async def maybe_draft(self, signal: Signal) -> None:
        """Публикует находку в X сразу, без подтверждения владельцем.

        "Черновик" в имени — по истории (раньше правда спрашивал), менять
        название без необходимости не стал. approve() ниже делает всю
        настоящую работу (лимит, публикация, разбор ошибок X) — эта
        функция только решает, писать ли текст и запускает её сама,
        не дожидаясь нажатия кнопки, которой больше нет.
        """
        opensea_url = (
            marketplace_url(self._chain, signal.contract_address)
            if signal.contract_address
            else None
        )
        template = build_caption(
            source=signal.source,
            kind=signal.kind,
            text=signal.text,
            opensea_url=opensea_url,
        )
        if template is None:
            return

        # К модели попадает только то, что build_caption уже признал
        # постабельным: сигналы из соцсетей до неё не доходят вообще.
        caption, should_post = await self._generated_caption(signal.text, opensea_url)
        if not should_post:
            return
        caption = caption or template

        post_id = await self._db.create_pending_post(
            address=signal.contract_address, text=caption, kind="post"
        )
        error = await self.approve(post_id)
        if error:
            log.warning(
                "автопост не вышел (%s), черновик #%s остался нерассмотренным", error, post_id
            )

    async def maybe_quote(self, signal: Signal) -> None:
        """Цитирует чужой твит с коротким комментарием от себя.

        Отдельная, более осторожная активность: это не собственный
        проверенный факт, а реакция на чужой пост, поэтому отдельная
        дневная квота (max_quotes_per_day) — по умолчанию меньше, чем у
        обычных постов (а у тех теперь нет верхней границы вовсе).
        """
        if signal.source != "twitter" or signal.kind not in QUOTABLE_KINDS:
            return
        if not signal.url:
            return

        comment = await self._generated_quote_comment(signal.text)
        if comment is None:
            # Решила пропустить, не ответила вовремя, недоступна, или сам
            # владелец выключил ИИ — молчим, а не постим пустое: в отличие
            # от caption_prompt, у цитирования нет шаблонного запасного
            # текста (см. caption.py).
            return

        post_id = await self._db.create_pending_post(
            address=signal.contract_address, text=comment, kind="quote", quoted_url=signal.url
        )
        error = await self.approve(post_id)
        if error:
            log.warning(
                "цитирование не вышло (%s), черновик #%s остался нерассмотренным",
                error,
                post_id,
            )

    async def approve(self, post_id: int) -> str | None:
        """Публикует черновик (пост или цитирование). Возвращает текст

        ошибки для показа владельцу, либо None при успехе.
        """
        post = await self._db.get_post(post_id)
        if post is None or post["status"] != "pending":
            return "Этот черновик уже не актуален."

        kind = post.get("kind") or "post"
        if await self._quota_used(kind):
            await self._db.decide_post(post_id, status="skipped")
            label = "постов" if kind == "post" else "цитирований"
            return f"Дневной лимит {label} в X уже исчерпан."

        try:
            if kind == "quote":
                tweet_url = await self._twitter.quote(post["text"], post["quoted_url"])
            else:
                tweet_url = await self._twitter.post(post["text"])
        except Exception as exc:
            log.exception("не удалось опубликовать пост %s в X", post_id)
            # X сам отвечает понятным текстом на свои же отказы (дубликат
            # текста, лимит запросов, заблокированный аккаунт) — если это
            # такой случай, показываем его прямо, а не только "смотри лог":
            # владельцу для дубликата достаточно просто поменять текст, а
            # не разбираться в traceback'е ради этого.
            try:
                from twikit.errors import TwitterException
            except ImportError:
                TwitterException = ()  # noqa: N806 — twikit не установлен
            if isinstance(exc, TwitterException):
                return f"X отказал: {exc}"
            return "Не получилось опубликовать — подробности в логе."

        await self._db.decide_post(post_id, status="posted", tweet_url=tweet_url)
        return None

    async def send_test_draft(self, text: str | None = None) -> int:
        """Черновик по прямому запросу владельца, минуя сигналы и любые пороги.

        approve() ниже всё равно проверит квоту перед реальной публикацией
        (для kind='post' она теперь может быть не задана вовсе). Это
        осознанный ручной обход создания черновика: владелец явно просит
        проверить публикацию прямо сейчас, а не очередная находка.

        Без своего текста — генерируется с меткой времени, а не постоянная
        строка: X отклоняет повторную публикацию буквально того же текста
        как дубликат (twikit.errors.DuplicateTweet), и вторая проверка
        подряд с тем же дефолтным текстом падала бы именно на этом, а не
        проверяла бы то, что реально нужно проверить.
        """
        if not text:
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            text = f"Тестовый пост — проверяем публикацию в X ({stamp} UTC)."
        post_id = await self._db.create_pending_post(address=None, text=text, kind="post")
        await self._notify_owner(post_id, text)
        return post_id

    async def skip(self, post_id: int) -> None:
        post = await self._db.get_post(post_id)
        if post is not None and post["status"] == "pending":
            await self._db.decide_post(post_id, status="skipped")

    async def _generated_caption(
        self, fact: str, opensea_url: str | None
    ) -> tuple[str | None, bool]:
        """(текст_или_None, стоит_ли_публиковать).

        None + True — модель решила, что писать стоит, но сам текст не
        прошёл проверку (придумала цифру, вставила ссылку) — используем
        шаблон, вопрос был только к конкретным словам, не к находке.
        None + False — явный SKIP, модель недоступна или не ответила
        вовремя: решение или неопределённость про саму находку,
        публиковать не стоит вообще, шаблон тут не поможет.

        Если ИИ выключен в настройках целиком — это другой случай: порог
        просто неприменим (нечем оценивать), а не повод молчать, так
        было и до его появления — публикуем шаблон как есть (None, True).
        """
        if self._llm is None:
            return None, True

        budget = body_budget(opensea_url)
        system, user = caption_prompt(fact, budget)
        try:
            raw = await asyncio.wait_for(
                self._llm.chat(system, user, max_tokens=120), timeout=_LLM_TIMEOUT_S
            )
        except Exception:
            log.warning("не удалось оценить находку перед публикацией — пропускаю", exc_info=True)
            return None, False

        if is_skip_reply(raw):
            log.info("модель сочла находку рядовой, не публикую: %s", fact[:70])
            return None, False

        body = clean_generated_body(raw, fact=fact, budget=budget)
        if body is None:
            if raw:
                log.info("текст от модели не прошёл проверку, беру шаблонный: %r", raw[:80])
            return None, True
        return compose(body, opensea_url), True

    async def _generated_quote_comment(self, tweet_text: str) -> str | None:
        """Комментарий к чужому твиту, либо None — без запасного шаблона

        (SKIP, недоступность модели, тайм-аут и невалидный ответ здесь
        неотличимы для вызывающего кода — во всех случаях просто молчим).
        """
        if self._llm is None:
            return None

        system, user = quote_prompt(tweet_text)
        try:
            raw = await asyncio.wait_for(
                self._llm.chat(system, user, max_tokens=80), timeout=_LLM_TIMEOUT_S
            )
        except Exception:
            log.warning(
                "не удалось оценить твит перед цитированием — пропускаю", exc_info=True
            )
            return None

        if is_skip_reply(raw):
            return None

        body = clean_generated_body(raw, fact=tweet_text, budget=quote_budget())
        if body is None and raw:
            log.info("комментарий от модели не прошёл проверку, не цитирую: %r", raw[:80])
        return body

    async def _quota_used(self, kind: str) -> bool:
        limit = self._max_per_day if kind == "post" else self._max_quotes_per_day
        if limit is None:
            return False
        since = datetime.now(timezone.utc) - timedelta(days=1)
        return await self._db.count_posts_since(since, kind=kind) >= limit
