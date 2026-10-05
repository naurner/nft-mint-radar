"""Периодический опрос уже найденных коллекций.

Раз в интервал перечитывает состояние контракта в сети (и OpenSea, если
настроен ключ), сохраняет снимок в историю и правит исходное сообщение
в каждом чате, где оно было отправлено, — вместо того чтобы присылать
новое. Слежение останавливается само: коллекция распродана, цифры не
меняются много проверок подряд, или истёк отведённый на неё срок —
вечно опрашивать заброшенный контракт незачем.

Сборка карточки (`_build_card`) переиспользуется в двух местах: тут же,
в периодической проверке, и в команде /find — там нужны те же цифры
по требованию, а не по расписанию.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from ..chain.contracts import ContractInspector
from ..enrich.models import ProjectLinks, marketplace_url
from ..enrich.service import build_explorer_url
from ..models import Signal, looks_like_junk_contract
from .scoring import compute_interest_score

log = logging.getLogger(__name__)

_EMPTY_STATS = {
    "floor_price": None,
    "floor_symbol": "ETH",
    "num_owners": None,
    "total_volume": None,
    "volume_symbol": "ETH",
    "total_sales": None,
    "average_price": None,
}

# Привязаны к доллару 1:1 по определению — курс для них не нужен, в отличие
# от ETH, где нужен внешний курс с CoinGecko. Список — крупные, реально
# встречающиеся стейблкоины, а не попытка перечислить вообще все.
_USD_STABLECOINS = {"USDC", "USDT", "USDG", "DAI", "BUSD", "USDP", "TUSD", "FDUSD", "PYUSD"}

# (минимальная доля саплая, множитель к refresh_interval_s) — по убыванию
# порога, первое подходящее правило и применяется. Чем ближе к распродаже,
# тем чаще смотрим; коллекцию, которую разбирают по 1-2% в час, незачем
# трогать вообще — ей никто особо не интересуется.
_FILL_INTERVAL_TIERS = (
    (0.90, 0.2),
    (0.50, 1.0),
    (0.10, 3.0),
    (0.0, 12.0),
)


class LiveTracker:
    def __init__(
        self,
        db,
        client,
        editor,
        *,
        opensea=None,
        explorer_url: str = "",
        explorer_token_path: str = "/token/{address}",
        marketplace_chain: str = "robinhood",
        refresh_interval_s: float = 300.0,
        poll_interval_s: float = 60.0,
        max_duration_hours: float = 48.0,
        stale_after_checks: int = 12,
        check_delay_s: float = 0.5,
        sales_cache_ttl_s: float = 900.0,
        website=None,
        website_price_cache_ttl_s: float = 3600.0,
        pricing=None,
        clock=time.monotonic,
        now=lambda: datetime.now(timezone.utc),
        on_signal=None,
        near_sellout_threshold: float = 0.9,
    ):
        self._db = db
        self._inspector = ContractInspector(client)
        self._editor = editor
        self._opensea = opensea
        self._website = website
        # Отдельный алерт-пинг (не просто правка карточки) на всю очередь
        # уведомлений — «почти распродано» должно разбудить телефон, а
        # правка сообщения в Telegram уведомление не шлёт.
        self._on_signal = on_signal
        self._near_sellout_threshold = near_sellout_threshold
        self._explorer_url = explorer_url
        self._explorer_token_path = explorer_token_path
        self._marketplace_chain = marketplace_chain
        # refresh_interval_s — обычный темп проверки (базовая ставка в
        # _dynamic_interval); poll_interval_s — как часто сам цикл вообще
        # просыпается взглянуть, кому пора. Разделены специально: коллекция
        # у порога распродажи должна проверяться чаще базовой ставки, а для
        # этого цикл должен просыпаться чаще неё, а не только с её периодом.
        self._interval = refresh_interval_s
        self._poll_interval = poll_interval_s
        self._max_duration = timedelta(hours=max_duration_hours)
        self._stale_after = stale_after_checks
        # Пауза между проектами внутри одного тика — иначе N проектов подряд
        # без задержки залпом бьют по общему лимиту RPC/OpenSea, и часть
        # карточек в этом тике молча остаётся необновлённой.
        self._check_delay = check_delay_s
        # Последняя/макс. продажа не обязаны быть свежее пяти минут — а
        # отдельный запрос к OpenSea на КАЖДЫЙ тик КАЖДОГО проекта удваивает
        # нагрузку на её лимит частоты сверх того, что уже уходит на floor/
        # объём. Кешируем результат на sales_cache_ttl_s и не спрашиваем
        # заново, пока он не протух — /find всегда обходит кеш, там это
        # разовый запрос по требованию, а не непрерывный опрос.
        self._sales_cache_ttl = sales_cache_ttl_s
        self._sales_cache: dict[str, tuple[dict | None, float]] = {}
        # Сайт/соцсети проекта часто не удаётся получить в момент самого
        # первого алерта — OpenSea мог ещё не проиндексировать совсем новый
        # контракт (это и есть смысл находить раньше него). До этой правки
        # enrich/service.py::Enricher пробовал это ровно один раз, и если
        # тогда не получилось — website оставался NULL навсегда, даже когда
        # slug уже найден и OpenSea давно всё знает. Теперь пробуем повторно
        # здесь же, пока не найдётся хоть что-то — с той же паузой между
        # попытками, что и у sales_cache, чтобы не долбить OpenSea навечно
        # ради коллекции, у которой просто нет ни одной соцсети.
        self._links_attempted: dict[str, float] = {}
        # Резервная цена минта с сайта проекта — только когда контракт её
        # сам не выдал ни одним из угадываемых геттеров. Сайт не меняет
        # цену минта посреди раздачи, поэтому кешируем надолго и не долбим
        # чужой сервер каждые пять минут ради того же самого текста.
        self._website_price_cache_ttl = website_price_cache_ttl_s
        self._website_price_cache: dict[str, tuple[float | None, float]] = {}
        self._clock = clock
        self._now = now
        # Перевод цены минта в доллары — необязателен, без него просто нет строки.
        self._pricing = pricing

    async def track(self, address: str) -> None:
        """Включает слежение за адресом и сразу делает первую проверку.

        Без немедленной проверки самое первое сообщение о проекте
        оставалось бы в виде одноразового алерта до пяти минут — ровно
        до ближайшего планового тика, без всякой причины ждать.
        """
        started = await self._db.start_tracking(address)
        if not started:
            return
        project = await self._db.get_project(address)
        if project is None:
            return
        try:
            await self._check_one(project)
        except Exception:
            log.exception("не удалось сделать первую проверку для %s", address)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("цикл слежения споткнулся")

    async def tick(self) -> None:
        """Проверяет только тех, кому реально пора — не всех подряд.

        Частота своя у каждого проекта: чем ближе к распродаже, тем чаще
        нужно смотреть, чтобы вовремя предупредить (см. _dynamic_interval).
        Коллекцию, которую разбирают по 1-2% в час, незачем тревожить
        каждые пять минут — лишняя проверка для неё просто лишняя нагрузка
        на RPC/OpenSea без всякой пользы.
        """
        projects = await self._db.active_tracked_projects()
        now = self._now()
        checked = 0
        for project in projects:
            if not await self._is_due(project, now):
                continue
            if checked and self._check_delay:
                await asyncio.sleep(self._check_delay)
            checked += 1
            try:
                await self._check_one(project)
            except Exception:
                log.exception("не удалось обновить %s", project["address"])

    async def _is_due(self, project: dict, now: datetime) -> bool:
        last_checked = _parse(project.get("last_checked_at"))
        if last_checked is None:
            return True  # ни разу не проверяли — точно пора

        snapshot = await self._db.latest_snapshot(project["address"])
        fill = _fill_ratio(snapshot) if snapshot else None
        interval = self._dynamic_interval(fill)
        return (now - last_checked).total_seconds() >= interval

    def _dynamic_interval(self, fill: float | None) -> float:
        """Насколько часто проверять этот конкретный проект, в секундах.

        Без данных о заполненности (ни одного снимка с известным
        max_supply) — обычный темп, refresh_interval_s. Пороги и множители
        (см. _FILL_INTERVAL_TIERS) — сознательное решение, не то, что стоит
        гонять владельца настраивать.
        """
        if fill is None:
            return self._interval
        for threshold, multiplier in _FILL_INTERVAL_TIERS:
            if fill >= threshold:
                return self._interval * multiplier
        return self._interval

    async def _check_one(self, project: dict) -> None:
        address = project["address"]
        now = self._now()

        started = _parse(project.get("tracking_started_at"))
        if started is not None and now - started > self._max_duration:
            await self._db.set_tracking_status(address, "expired")
            return

        info, stats, sold_out = await self._fetch_state(project)

        if looks_like_junk_contract(info.name, info.symbol, info.total_supply):
            # Прошёл первичный отсев (sources/filters.py), но на живых данных
            # оказался тем же самым: контрактом без внятного имени или с
            # абсурдным саплаем — обычно ложное "да" на supportsInterface у
            # того, что на самом деле не NFT. Не бесконечно висеть мусором
            # в чате — останавливаем слежение и объясняем, почему.
            await self._db.set_tracking_status(address, "invalid")
            label = info.name or info.symbol or address
            text = (
                f"❌ <b>{html.escape(label)}</b>\n"
                "Похоже, это не настоящая NFT-коллекция — слежение остановлено.\n\n"
                f"<code>{address}</code>"
            )
            for chat_id, message_id in await self._db.threads_for_address(address):
                await self._editor.edit(chat_id, message_id, address, text)
            return

        sales = await self._recent_sales(project)

        await self._maybe_alert_near_sellout(project, info, now)

        latest = await self._db.latest_snapshot(address)
        changed = latest is None or (
            latest.get("total_supply") != info.total_supply
            or latest.get("floor_price_eth") != stats["floor_price"]
        )

        await self._db.add_snapshot(
            address=address,
            ts=now,
            total_supply=info.total_supply,
            max_supply=info.max_supply or project.get("max_supply"),
            mint_price_wei=(
                info.mint_price_wei
                if info.mint_price_wei is not None
                else project.get("mint_price_wei")
            ),
            floor_price_eth=stats["floor_price"],
            floor_price_symbol=stats["floor_symbol"] if stats["floor_price"] is not None else None,
            unique_minters=project.get("unique_minters"),
            num_owners=stats["num_owners"],
            source="tracker",
        )

        history = await self._db.snapshot_history(address, limit=200)
        score, reason = compute_interest_score(project, history)
        await self._db.set_interest_score(address, score, reason)

        if changed or sold_out:
            await self._db.reset_stale_checks(address)
            text = await self._render(project, info, stats, now, sold_out, sales=sales)
            for chat_id, message_id in await self._db.threads_for_address(address):
                await self._editor.edit(chat_id, message_id, address, text)
        else:
            stale = await self._db.bump_stale_checks(address)
            if stale >= self._stale_after:
                await self._db.set_tracking_status(address, "stale")

        if sold_out:
            await self._db.set_tracking_status(address, "sold_out")

    async def _maybe_alert_near_sellout(self, project: dict, info, now) -> None:
        """Разовый пинг, когда раздача подходит к концу — не просто правка

        карточки: правка сообщения в Telegram не будит телефон, а тут как
        раз тот момент, когда важно не пропустить. Шлётся не больше одного
        раза за жизнь проекта (см. near_sellout_alerted), иначе долбило бы
        на каждом тике, пока прогресс держится выше порога.
        """
        if self._on_signal is None or project.get("near_sellout_alerted"):
            return
        if info.total_supply is None:
            return
        max_supply = info.max_supply or project.get("max_supply")
        if not max_supply:
            return

        fill = info.total_supply / max_supply
        if fill < self._near_sellout_threshold or fill >= 1.0:
            # >= 1.0 — уже распродано, а не "подходит к распродаже": об этом
            # уже сообщает отдельная правка карточки на "Распродано" ниже.
            # Без этой проверки "почти распродано" могло прийти ПОСЛЕ того,
            # как коллекция уже полностью разошлась — например, если самая
            # первая проверка нового проекта застала его уже на 100%.
            return

        label = project.get("name") or project.get("symbol") or project["address"]
        pct = min(int(fill * 100), 100)
        await self._on_signal(
            Signal(
                source="onchain",
                kind="mint_near_sellout",
                # По-английски, как и mint_surge (см. sources/onchain.py) —
                # этот же текст уходит и в caption_prompt/quote_prompt, где
                # вся остальная инструкция на английском: смешение языков
                # сбивало локальную модель с толку (почти всегда SKIP) чаще,
                # чем помогало. "Near sellout:" впереди — та же явная подсказка
                # значимости, что и "Mint surge:" у соседнего вида сигнала.
                text=f"Near sellout: {label} — {info.total_supply}/{max_supply} ({pct}%) minted",
                url=None,
                author=None,
                contract_address=project["address"],
                chain=project.get("chain"),
                timestamp=now,
                raw={"total_supply": info.total_supply, "max_supply": max_supply},
            )
        )
        # Отмечаем ПОСЛЕ отправки: если что-то упадёт выше, следующий тик
        # попробует ещё раз, а не молча забудет об этом навсегда.
        await self._db.mark_near_sellout_alerted(project["address"])

    async def render_card(self, project: dict, *, recent_sales: bool = False) -> str:
        """Карточка по требованию — то, что показывает команда /find.

        В отличие от периодической проверки, не пишет снимок в историю
        и не двигает статус слежения: это разовый взгляд на текущее
        состояние, а не тик планового опроса.
        """
        info, stats, sold_out = await self._fetch_state(project)
        sales = await self._recent_sales(project, fresh=True) if recent_sales else None

        return await self._render(
            project, info, stats, self._now(), sold_out, sales=sales
        )

    async def _recent_sales(self, project: dict, *, fresh: bool = False) -> dict | None:
        if self._opensea is None:
            return None
        slug = project.get("opensea_slug")
        if not slug:
            return None

        if not fresh:
            cached = self._sales_cache.get(slug)
            if cached is not None and self._clock() - cached[1] < self._sales_cache_ttl:
                return cached[0]

        result = await self._opensea.collection_recent_sales(slug)
        self._sales_cache[slug] = (result, self._clock())
        return result

    async def _fetch_state(self, project: dict):
        """Живое состояние контракта: метаданные + статистика OpenSea."""
        info = await self._inspector.inspect(project["address"])
        info = await self._inspector.with_pricing(info)
        stats = await self._opensea_stats(project)
        sold_out = bool(
            info.max_supply
            and info.total_supply is not None
            and info.total_supply >= info.max_supply
        )
        return info, stats, sold_out

    async def _opensea_stats(self, project: dict) -> dict:
        """Флор, держатели, объём, число продаж — из одного и того же ответа.

        Всё это отдаёт один и тот же вызов collection_stats(), так что
        добавление новых полей не стоит ни одного лишнего запроса.
        """
        if self._opensea is None:
            return dict(_EMPTY_STATS)

        slug = project.get("opensea_slug")
        if slug is None:
            # Свежий контракт мог не быть проиндексирован в момент первого
            # алерта. Пробуем на каждой проверке, пока не найдётся, — это
            # один лёгкий запрос раз в несколько минут, не накладно.
            slug = await self._opensea.collection_slug(project["address"])
            if slug:
                await self._db.set_opensea_slug(project["address"], slug)

        if not slug:
            return dict(_EMPTY_STATS)

        if not _has_social_links(project):
            await self._backfill_links(project, slug)

        stats = await self._opensea.collection_stats(slug)
        return stats or dict(_EMPTY_STATS)

    async def _backfill_links(self, project: dict, slug: str) -> None:
        """Повторная попытка получить сайт/соцсети теперь, когда slug уже

        известен — с первой попытки (в момент самого первого алерта)
        OpenSea мог ещё не проиндексировать контракт, и без этой повторной
        попытки website оставался бы NULL навсегда. Правит `project` на
        месте, чтобы РЕЗЕРВ цены минта с сайта (ниже) увидел его в этом же
        тике, а не только со следующего.
        """
        address = project["address"]
        last_try = self._links_attempted.get(address)
        if last_try is not None and self._clock() - last_try < self._sales_cache_ttl:
            return
        self._links_attempted[address] = self._clock()

        links = await self._opensea.collection_links(slug)
        if not links.has_socials:
            return

        await self._db.save_links(address, links)
        project["website"] = project.get("website") or links.website
        project["twitter"] = project.get("twitter") or links.twitter
        project["discord"] = project.get("discord") or links.discord
        project["telegram"] = project.get("telegram") or links.telegram

    async def _render(
        self, project: dict, info, stats: dict, now, sold_out: bool, *, sales=None
    ) -> str:
        from ..notify.tracking import format_tracking_card

        links = ProjectLinks(
            explorer=build_explorer_url(
                self._explorer_url, self._explorer_token_path, project["address"]
            ),
            opensea=project.get("opensea_url")
            or marketplace_url(self._marketplace_chain, project["address"]),
            twitter=project.get("twitter"),
            discord=project.get("discord"),
            telegram=project.get("telegram"),
            website=project.get("website"),
        )

        price = info.mint_price_wei
        if price is None:
            price = project.get("mint_price_wei")

        website_price = None
        if price is None:
            website_price = await self._website_mint_price(project, links)

        usd_price = None
        if self._pricing is not None:
            if price is not None:
                usd_price = await self._pricing.usd_line(price)
            elif website_price is not None:
                usd_price = await self._pricing.usd_amount(website_price)

        sales = sales or {}
        floor_symbol = stats.get("floor_symbol") or "ETH"
        volume_symbol = stats.get("volume_symbol") or "ETH"
        last_sale_symbol = sales.get("last_sale_symbol") or "ETH"
        highest_sale_symbol = sales.get("highest_sale_symbol") or "ETH"

        return format_tracking_card(
            project,
            info,
            links,
            floor=stats["floor_price"],
            floor_symbol=floor_symbol,
            floor_usd=await self._usd(stats["floor_price"], floor_symbol),
            num_owners=stats["num_owners"],
            total_volume=stats.get("total_volume"),
            volume_symbol=volume_symbol,
            volume_usd=await self._usd(stats.get("total_volume"), volume_symbol),
            total_sales=stats.get("total_sales"),
            average_price=stats.get("average_price"),
            average_usd=await self._usd(stats.get("average_price"), volume_symbol),
            last_sale=sales.get("last_sale"),
            last_sale_symbol=last_sale_symbol,
            last_sale_usd=await self._usd(sales.get("last_sale"), last_sale_symbol),
            highest_sale=sales.get("highest_sale"),
            highest_sale_symbol=highest_sale_symbol,
            highest_sale_usd=await self._usd(sales.get("highest_sale"), highest_sale_symbol),
            now=now,
            sold_out=sold_out,
            usd_price=usd_price,
            mint_price_eth_from_site=website_price,
        )

    async def _website_mint_price(self, project: dict, links: ProjectLinks) -> float | None:
        """Резерв на случай, когда ни один угадываемый геттер цены не сработал.

        Кешируется надолго (см. website_price_cache_ttl_s) — сайт не меняет
        цену минта посреди раздачи, и нет причины скрести его каждый тик.
        """
        if self._website is None or not links.website:
            return None

        address = project["address"]
        cached = self._website_price_cache.get(address)
        if cached is not None and self._clock() - cached[1] < self._website_price_cache_ttl:
            return cached[0]

        result = await self._website.mint_price_eth(links.website)
        self._website_price_cache[address] = (result, self._clock())
        return result

    async def _usd(self, amount: float | None, symbol: str) -> str | None:
        """Переводит в доллары то, для чего у нас есть основание это делать.

        Курс ETH/USD с CoinGecko подходит только ETH/WETH — для прочего он
        был бы враньём. Но доллар-стейблкоины (USDG и подобные) устроены
        иначе: они и так привязаны к доллару 1:1 по определению, курс тут
        не нужен вовсе, поэтому переводятся всегда, даже без pricing.
        Для любой другой валюты (неизвестный ERC-20) курса у нас нет —
        показать оценку значило бы её выдумать.
        """
        if amount is None:
            return None
        symbol_upper = (symbol or "").upper()
        if symbol_upper in _USD_STABLECOINS:
            return f"${amount:.2f}"
        if symbol_upper in ("ETH", "WETH") and self._pricing is not None:
            return await self._pricing.usd_amount(amount)
        return None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _has_social_links(project: dict) -> bool:
    return any(project.get(k) for k in ("website", "twitter", "discord", "telegram"))


def _fill_ratio(snapshot: dict) -> float | None:
    total = snapshot.get("total_supply")
    max_supply = snapshot.get("max_supply")
    if total is None or not max_supply:
        return None
    return total / max_supply
