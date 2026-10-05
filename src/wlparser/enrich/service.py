"""Сборка ссылок по проекту с кешем.

Ссылка на эксплорер строится локально и есть всегда. Остальное тянется
из OpenSea один раз на контракт: повторные сигналы по тому же проекту
берут результат из кеша, а не дёргают сеть заново.
"""

from __future__ import annotations

import asyncio
import logging

from .models import ProjectLinks, marketplace_url

log = logging.getLogger(__name__)


def build_explorer_url(base: str, template: str, address: str) -> str | None:
    if not base:
        return None
    return base.rstrip("/") + template.format(address=address)


class Enricher:
    def __init__(
        self,
        opensea=None,
        *,
        explorer_url: str = "",
        explorer_token_path: str = "/token/{address}",
        marketplace_chain: str = "robinhood",
        timeout_s: float = 8.0,
        store=None,
    ):
        self._opensea = opensea
        self._explorer_url = explorer_url
        self._explorer_token_path = explorer_token_path
        self._marketplace_chain = marketplace_chain
        self._timeout = timeout_s
        self._store = store
        self._cache: dict[str, ProjectLinks] = {}

    async def enrich(self, address: str | None) -> ProjectLinks:
        if not address:
            return ProjectLinks()

        address = address.lower()
        # Строится локально и доступно всегда, даже когда внешние API молчат.
        explorer = ProjectLinks(
            explorer=build_explorer_url(
                self._explorer_url, self._explorer_token_path, address
            ),
            opensea=marketplace_url(self._marketplace_chain, address),
        )

        cached = self._cache.get(address)
        if cached is not None:
            return cached.merged_with(explorer)

        remote = await self._lookup_remote(address)
        links = remote.merged_with(explorer)
        self._cache[address] = links

        if self._store is not None and remote.has_socials:
            try:
                await self._store(address, links)
            except Exception:
                log.exception("не удалось сохранить ссылки проекта %s", address)

        return links

    async def _lookup_remote(self, address: str) -> ProjectLinks:
        """Внешний источник не должен задерживать алерт.

        Лучше отправить сообщение без ссылки на OpenSea, чем задержать
        уведомление о минте на время таймаута.
        """
        if self._opensea is None:
            return ProjectLinks()
        try:
            return await asyncio.wait_for(
                self._opensea.lookup(address), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            log.warning("OpenSea не ответил за %.0f с для %s", self._timeout, address)
        except Exception:
            log.exception("ошибка обращения к OpenSea для %s", address)
        return ProjectLinks()
