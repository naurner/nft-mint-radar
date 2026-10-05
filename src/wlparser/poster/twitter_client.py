"""Публикация твита от аккаунта, уже авторизованного для чтения (twscrape).

Идея: twscrape умеет только читать (поиск, таймлайны) — у него в принципе
нет метода публикации. Но обе библиотеки авторизуются одинаково — парой
cookies `auth_token`/`ct0` того же самого аккаунта, — поэтому вместо
повторного логина эти cookies просто передаются в twikit.

Проверено вживую (см. git log этого файла): реальные cookies упирались
сперва в баг KEY_BYTE (см. _patch_transaction_indices), затем — уже после
того, как твит фактически уходил в X — в KeyError при разборе ответа
(см. _patch_user_fields). Оба обхода нужны одновременно; без любого из
них `post()` либо не достучится до X, либо отчитается об ошибке про уже
опубликованный твит.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_transaction_patched = False
_user_patched = False


# Второй, независимо предложенный фикс той же поломки (комментарий
# AkaraiRP в том же issue) — с обязательной запятой перед chunk id и
# только hex-хешем, в отличие от PR #416 ниже. Раз оба варианта гуляют
# как рабочие в одном и том же тике проблемы, неизвестно, какой формат
# отдаёт X именно сейчас — пробуем оба, прежде чем сдаться.
_LEGACY_CHUNK_RE = re.compile(r',(\d+):["\']ondemand\.s["\']')


def _patch_transaction_indices() -> None:
    """Обходит баг в twikit 2.3.3 (текущий релиз на PyPI, следующего нет):
    в апреле 2026 X сменила формат webpack-чанков на странице, и регулярка
    библиотеки для поиска файла ondemand.s.js перестала совпадать — любая
    публикация падает с "Couldn't get KEY_BYTE indices". У апстрима есть
    открытый PR с исправлением, автор проверил его живым тестом против
    x.com (github.com/d60/twikit/pull/416), но релиза с ним нет уже
    полгода — патчим тот же метод сами, тем же кодом, плюс второй
    независимый вариант фикса из того же issue как запасной (см.
    _LEGACY_CHUNK_RE). Старый формат пробуется первым, так что если X
    всё же откатит изменение, ничего не ломается. Если внутренности
    twikit в будущей версии поменяются и патч не встанет — просто лог,
    а не сорванный импорт.
    """
    global _transaction_patched
    if _transaction_patched:
        return

    try:
        from twikit.x_client_transaction import transaction as tx

        chunk_name_re = re.compile(r'(\d+):"ondemand\.s"')

        def _find_file_hash(response_str: str) -> str | None:
            on_demand_file = tx.ON_DEMAND_FILE_REGEX.search(response_str)
            if on_demand_file:
                return on_demand_file.group(1)

            chunk_id_match = chunk_name_re.search(response_str)
            if chunk_id_match:
                hash_pattern = re.compile(rf'{chunk_id_match.group(1)}:"([\w]+)"')
                for m in hash_pattern.finditer(response_str):
                    if m.group(1) != "ondemand" and len(m.group(1)) <= 12:
                        return m.group(1)

            legacy_match = _LEGACY_CHUNK_RE.search(response_str)
            if legacy_match:
                legacy_hash = re.compile(rf',{legacy_match.group(1)}:"([0-9a-f]+)"')
                found = legacy_hash.search(response_str)
                if found:
                    return found.group(1)

            return None

        async def _patched_get_indices(self, home_page_response, session, headers):
            key_byte_indices: list[str] = []
            response = self.validate_response(home_page_response) or self.home_page_response
            response_str = str(response)

            file_hash = _find_file_hash(response_str)
            if file_hash:
                url = (
                    "https://abs.twimg.com/responsive-web/client-web/"
                    f"ondemand.s.{file_hash}a.js"
                )
                file_response = await session.request(method="GET", url=url, headers=headers)
                for item in tx.INDICES_REGEX.finditer(str(file_response.text)):
                    key_byte_indices.append(item.group(2))

            if not key_byte_indices:
                # Ничего не подошло — логируем кусок страницы, а не только
                # факт неудачи: без этого следующая попытка починки снова
                # будет гаданием, а не чтением того, что реально прислал X
                # (обычный ответ, страница логина, капча — большая разница).
                log.error(
                    "twikit: не нашёл индексы ни одним из трёх паттернов; "
                    "длина ответа %d, начало: %r",
                    len(response_str),
                    response_str[:500],
                )
                raise Exception("Couldn't get KEY_BYTE indices")
            indices = list(map(int, key_byte_indices))
            return indices[0], indices[1:]

        tx.ClientTransaction.get_indices = _patched_get_indices
        _transaction_patched = True
    except Exception:
        log.exception(
            "не удалось наложить патч на twikit (github.com/d60/twikit/pull/416),"
            " публикация может не работать"
        )


def _patch_user_fields() -> None:
    """Обходит второй баг, найденный тем же способом что и KEY_BYTE (см.
    live-лог владельца): X не всегда присылает entities.description.urls,
    pinned_tweet_ids_str и withheld_in_countries в профиле пользователя —
    например, ровно у аккаунта без ссылок в био, — а twikit читает их
    прямым ['...'], а не .get(...). Твит к этому моменту уже реально
    опубликован: KeyError падает ПОСЛЕ успешной отправки, только пока
    twikit собирает Python-объект ответа, поэтому бот раньше отчитывался
    "не удалось", хотя твит на самом деле ушёл. Тот же баг и починка есть
    в открытом PR github.com/d60/twikit/pull/432 (автор проверил живыми
    cookies). Патч не переписывает __init__ целиком — только подкладывает
    отсутствующие ключи с безопасными значениями перед вызовом настоящего
    __init__, чтобы не разъехаться с ним при следующем обновлении twikit.
    """
    global _user_patched
    if _user_patched:
        return

    try:
        from twikit.user import User

        original_init = User.__init__

        def _patched_init(self, client, data: dict) -> None:
            legacy = data.get("legacy")
            if legacy is None:
                legacy = {}
                data["legacy"] = legacy
            legacy.setdefault("entities", {}).setdefault("description", {}).setdefault(
                "urls", []
            )
            legacy.setdefault("pinned_tweet_ids_str", [])
            legacy.setdefault("withheld_in_countries", [])
            original_init(self, client, data)

        User.__init__ = _patched_init
        _user_patched = True
    except Exception:
        log.exception(
            "не удалось наложить патч на twikit.User (github.com/d60/twikit/pull/432),"
            " публикация может падать с KeyError уже после реальной отправки твита"
        )


class TwikitPoster:
    def __init__(self, cookies: dict[str, str], *, language: str = "en-US"):
        self._cookies = cookies
        self._language = language
        self._client = None

    async def _client_or_create(self):
        if self._client is not None:
            return self._client

        _patch_transaction_indices()
        _patch_user_fields()

        from twikit import Client

        client = Client(self._language)
        client.set_cookies(self._cookies)
        self._client = client
        return client

    async def post(self, text: str) -> str | None:
        """Публикует твит, возвращает ссылку на него или None, если её не собрать."""
        client = await self._client_or_create()
        tweet = await client.create_tweet(text=text)
        return self._tweet_url(tweet)

    async def quote(self, text: str, tweet_url: str) -> str | None:
        """Цитирует чужой твит со своим комментарием — attachment_url встраивает

        цитируемый твит карточкой, как при цитировании руками в приложении.
        """
        client = await self._client_or_create()
        tweet = await client.create_tweet(text=text, attachment_url=tweet_url)
        return self._tweet_url(tweet)

    @staticmethod
    def _tweet_url(tweet) -> str | None:
        tweet_id = getattr(tweet, "id", None)
        username = getattr(getattr(tweet, "user", None), "screen_name", None)
        if not tweet_id or not username:
            log.warning("твит опубликован, но не удалось собрать на него ссылку")
            return None
        return f"https://x.com/{username}/status/{tweet_id}"
