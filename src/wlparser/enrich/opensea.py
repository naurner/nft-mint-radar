"""Клиент OpenSea API v2.

С сентября 2026 года все эти эндпоинты требуют API-ключ (раньше contract
и collections отвечали и без него). Без ключа методы этого класса просто
возвращают пустой результат — предупреждение об этом печатается один раз,
см. `_warn_about_key`.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .models import ProjectLinks, safe_url, twitter_url

log = logging.getLogger(__name__)

BASE = "https://api.opensea.io/api/v2"

# (status, json | None)
Fetch = Callable[[str, dict[str, str]], Awaitable[tuple[int, Any]]]


class OpenSeaClient:
    def __init__(self, fetch: Fetch, *, chain: str = "robinhood", api_key: str = ""):
        self._fetch = fetch
        self._chain = chain
        self._headers = {"accept": "application/json"}
        if api_key:
            self._headers["x-api-key"] = api_key
        # Про отсутствие ключа достаточно сказать один раз: иначе
        # предупреждение будет повторяться на каждой находке.
        self._warned_about_key = False

    def _warn_about_key(self) -> None:
        if self._warned_about_key:
            return
        self._warned_about_key = True
        log.warning(
            "OpenSea требует API-ключ: соцсети проектов в уведомления не попадут. "
            "Ключ бесплатный — opensea.io/account/developer, затем впиши его "
            "в config.yaml как links.opensea_api_key. "
            "Ссылка на саму коллекцию работает и без ключа."
        )

    async def collection_slug(self, address: str) -> str | None:
        """Отдаёт slug коллекции, если OpenSea уже проиндексировал контракт."""
        url = f"{BASE}/chain/{self._chain}/contract/{address}"
        status, data = await self._fetch(url, self._headers)
        if status != 200 or not isinstance(data, dict):
            if status in (401, 403):
                self._warn_about_key()
            elif status not in (404, 400):
                log.warning("OpenSea contract %s -> HTTP %s", address, status)
            return None
        slug = data.get("collection")
        return slug or None

    async def collection_links(self, slug: str) -> ProjectLinks:
        url = f"{BASE}/collections/{slug}"
        status, data = await self._fetch(url, self._headers)
        if status != 200 or not isinstance(data, dict):
            if status in (401, 403):
                self._warn_about_key()
            elif status != 404:
                log.warning("OpenSea collection %s -> HTTP %s", slug, status)
            return ProjectLinks()

        return ProjectLinks(
            opensea=safe_url(data.get("opensea_url"))
            or f"https://opensea.io/collection/{slug}",
            website=safe_url(data.get("project_url")),
            twitter=twitter_url(data.get("twitter_username")),
            discord=safe_url(data.get("discord_url")),
            telegram=safe_url(data.get("telegram_url")),
            collection_name=(data.get("name") or "").strip() or None,
            description=(data.get("description") or "").strip() or None,
            total_supply=data.get("total_supply"),
            collection_slug=slug,
        )

    async def collection_stats(self, slug: str) -> dict | None:
        """Флор, объём, число продаж и держателей — для живого слежения.

        Флор и объём хранят СВОЮ валюту отдельно (`floor_symbol`,
        `volume_symbol`): OpenSea не всегда считает их в ETH, для многих
        коллекций это стейблкоин вроде USDG — числа несравнимы напрямую,
        и подставлять «ETH» не глядя было бы враньём в сообщении.

        Средняя цена продажи (`average_price`) в самом ответе OpenSea
        отсутствует — считаем её сами как volume / sales, в той же
        валюте, что и объём.
        """
        url = f"{BASE}/collections/{slug}/stats"
        status, data = await self._fetch(url, self._headers)
        if status != 200 or not isinstance(data, dict):
            if status in (401, 403):
                self._warn_about_key()
            elif status != 404:
                log.warning("OpenSea stats %s -> HTTP %s", slug, status)
            return None

        total = data.get("total") or {}
        volume = _as_float(total.get("volume"))
        sales = _as_int(total.get("sales"))
        average = volume / sales if (volume is not None and sales) else None

        return {
            "floor_price": _as_float(total.get("floor_price")),
            "floor_symbol": total.get("floor_price_symbol") or "ETH",
            "num_owners": _as_int(total.get("num_owners")),
            "total_volume": volume,
            "volume_symbol": total.get("volume_symbol") or "ETH",
            "total_sales": sales,
            "average_price": average,
        }

    async def collection_recent_sales(self, slug: str, *, limit: int = 50) -> dict | None:
        """Последняя и максимальная цена среди последних `limit` продаж.

        Не "цена за всю историю": пролистать весь архив продаж коллекции
        ради одного числа было бы неоправданно ни по числу запросов, ни
        по времени ответа. `limit` продаж — разумный компромисс, и в
        сообщении это подписывается как оценка по недавним продажам,
        а не как настоящий исторический максимум.

        Каждая продажа несёт свою собственную валюту (`payment.symbol`) —
        разные лоты одной коллекции в принципе могут быть проданы за
        разные токены, поэтому у последней и у максимальной цены символ
        может отличаться, и обе величины возвращаются вместе со своим.
        """
        url = f"{BASE}/events/collection/{slug}?event_type=sale&limit={limit}"
        status, data = await self._fetch(url, self._headers)
        if status != 200 or not isinstance(data, dict):
            if status in (401, 403):
                self._warn_about_key()
            elif status != 404:
                log.warning("OpenSea events %s -> HTTP %s", slug, status)
            return None

        sales: list[tuple[object, float, str]] = []
        for event in data.get("asset_events") or []:
            parsed = _sale_price(event)
            if parsed is None:
                continue
            price, symbol = parsed
            timestamp = event.get("event_timestamp") or event.get("closing_date")
            sales.append((timestamp, price, symbol))

        if not sales:
            return None

        timed = [s for s in sales if s[0] is not None]
        # Порядок событий в ответе не гарантирован для наших целей, поэтому
        # "последняя" определяется по явной метке времени, если она есть.
        last_ts, last_price, last_symbol = max(timed, key=lambda s: s[0]) if timed else sales[0]
        _, highest_price, highest_symbol = max(sales, key=lambda s: s[1])

        return {
            "last_sale": last_price,
            "last_sale_symbol": last_symbol,
            "highest_sale": highest_price,
            "highest_sale_symbol": highest_symbol,
            "sample_size": len(sales),
        }

    async def lookup(self, address: str) -> ProjectLinks:
        """Полный путь: адрес -> slug -> ссылки. Пустой результат — норма.

        Свежий контракт OpenSea ещё не проиндексировал, и это ожидаемо:
        мы находим проекты раньше, чем маркетплейс.
        """
        slug = await self.collection_slug(address)
        if not slug:
            return ProjectLinks()
        return await self.collection_links(slug)


def _as_float(value) -> float | None:
    return float(value) if value is not None else None


def _as_int(value) -> int | None:
    return int(value) if value is not None else None


def _sale_price(event: dict) -> tuple[float, str] | None:
    payment = (event or {}).get("payment") or {}
    quantity = payment.get("quantity")
    if quantity is None:
        return None
    try:
        decimals = int(payment.get("decimals", 18))
        price = int(quantity) / (10**decimals)
    except (TypeError, ValueError):
        return None
    return price, payment.get("symbol") or "ETH"
