"""Сборка и запуск приложения.

Единственное место, где компоненты соединяются друг с другом —
вынесено из CLI, чтобы команда `run` оставалась тонкой.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from .chain.rpc import JsonRpcClient, http_transport
from .config import Settings
from .db import Database
from .enrich.opensea import OpenSeaClient
from .enrich.service import Enricher
from .enrich.website_price import WebsitePriceLookup
from .host_update import HostUpdater, format_result as format_update_result
from .llm import LocalLLM
from .quality import SignalQualityFilter
from .models import Signal
from .notify.commands import SubscriptionService, build_router
from .notify.digest import attach_takes, digest_since, format_digest, next_digest_at, parse_times
from .notify.keyboards import alert_keyboard, reminder_keyboard
from .notify.reminders import ReminderService
from .notify.telegram import TelegramBroadcaster
from .notify.tracking import LiveCardEditor
from .pipeline.noise import NoiseFilter
from .pipeline.runner import Pipeline
from .poster.service import Poster
from .poster.twitter_client import TwikitPoster
from .pricing import EthUsdRate, GasFeeEstimator, PriceConverter
from .sources.filters import ContractFilter
from .sources.onchain import OnchainSource
from .sources.twitter import TwitterSource
from .tracking.service import LiveTracker

log = logging.getLogger(__name__)

# Список под кнопкой "/" рядом с полем ввода — родной элемент интерфейса
# Telegram, дополняет постоянную клавиатуру с кнопкой "☰ Меню".
BOT_COMMANDS = [
    BotCommand(command="menu", description="Меню кнопками"),
    BotCommand(command="recent", description="Последние находки"),
    BotCommand(command="top", description="Что выглядит интереснее"),
    BotCommand(command="find", description="Найти проект по названию"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="stop", description="Отписаться"),
]


class BotAlreadyRunningError(RuntimeError):
    """Тот же токен уже опрашивает Telegram с другого устройства.

    Telegram отдаёт сообщения только одному экземпляру бота. Второй не просто
    работает хуже — он ломает первый, и оба перестают отвечать на команды
    и кнопки. Поэтому запуск прерывается с внятным текстом, а не тонет
    в потоке одинаковых ошибок.
    """


CURSOR_SAVE_INTERVAL_S = 10
# Раз в полминуты: напоминание с точностью до минуты этого достаточно.
REMINDER_TICK_S = 30


async def run_app(settings: Settings) -> None:
    db = Database.from_settings(settings)
    await db.connect()

    client = JsonRpcClient(
        http_transport(settings.chain.endpoints),
        min_request_interval_s=settings.chain.min_request_interval_s,
    )
    bot = Bot(token=settings.telegram_bot_token)
    http = httpx.AsyncClient(timeout=settings.links.timeout_s)
    # Объявлен до try: finally ниже закрывает его, и сбой на раннем этапе
    # не должен превращаться в NameError, маскирующий настоящую ошибку.
    llm: LocalLLM | None = None

    try:
        try:
            # Список команд под кнопкой "/" рядом с полем ввода — то же
            # самое меню, просто ещё и через родной элемент интерфейса
            # Telegram, а не только через постоянную клавиатуру снизу.
            await bot.set_my_commands(BOT_COMMANDS)
        except Exception:
            log.exception("не удалось задать список команд боту")

        start_block = await _resolve_start_block(db, client, settings)

        thread_cache = await db.load_threads()

        async def store_thread(chat_id: int, address: str, message_id: int) -> None:
            thread_cache[(chat_id, address)] = message_id
            await db.set_thread_id(chat_id, address, message_id)

        def lookup_thread(chat_id: int, address: str) -> int | None:
            return thread_cache.get((chat_id, address))

        # Один клиент на оба потребителя: иначе у каждого своя копия флага
        # «про ключ уже предупредили», и предупреждение в логе дублируется.
        opensea = _build_opensea(settings, http)

        enricher = Enricher(
            opensea,
            explorer_url=settings.chain.explorer_url,
            explorer_token_path=settings.chain.explorer_token_path,
            marketplace_chain=settings.links.opensea_chain,
            timeout_s=settings.links.timeout_s,
            store=db.save_links,
        )

        pricing = None
        if settings.pricing.enabled:

            async def _coingecko_fetch(url: str, headers: dict[str, str]):
                response = await http.get(url, headers=headers)
                try:
                    return response.status_code, response.json()
                except ValueError:
                    return response.status_code, None

            pricing = PriceConverter(
                EthUsdRate(
                    _coingecko_fetch,
                    cache_seconds=settings.pricing.refresh_interval_s,
                ),
                GasFeeEstimator(client, gas_per_mint=settings.pricing.gas_per_mint),
                include_gas=settings.pricing.include_gas,
            )

        owner_chat_id = int(settings.telegram_chat_id) if settings.telegram_chat_id else None

        async def tell_owner(text: str) -> None:
            if owner_chat_id is not None:
                await bot.send_message(
                    chat_id=owner_chat_id, text=text, parse_mode="HTML"
                )

        subscriptions = SubscriptionService(
            db, owner_chat_id=owner_chat_id, notify_owner=tell_owner
        )
        await subscriptions.ensure_owner()

        async def deliver_reminder(chat_id: int, text: str, row: dict) -> None:
            # Кнопка повтора нужна только на первом напоминании: у того,
            # что приходит за минуту, откладывать уже нечего.
            markup = reminder_keyboard(row["id"]) if row["stage"] == "lead" else None
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )

        reminders = ReminderService(
            db, deliver_reminder, lead_minutes=settings.alerts.reminder_lead_minutes
        )

        notifier = TelegramBroadcaster(
            bot,
            recipients=subscriptions.active_chat_ids,
            thread_lookup=lookup_thread,
            thread_store=store_thread,
            on_permanent_failure=subscriptions.deactivate,
            enricher=enricher,
            owner_chat_id=owner_chat_id,
            owner_head_start_s=settings.alerts.owner_head_start_s,
            keyboard_factory=alert_keyboard,
            pricing=pricing,
        )

        source = OnchainSource(
            client,
            chain_name=settings.chain.name,
            start_block=start_block,
            poll_interval=settings.chain.poll_interval_s,
            surge_threshold=settings.chain.surge_threshold,
            confirmations=settings.chain.confirmations,
            min_unique_minters=settings.mint.min_unique_minters,
            mint_window_minutes=settings.mint.window_minutes,
            log_range_blocks=settings.chain.log_range_blocks,
            max_lag_blocks=settings.chain.max_lag_blocks,
            contract_filter=ContractFilter(
                ignore_addresses=settings.mint.ignore_contracts,
                ignore_name_patterns=settings.mint.ignore_name_patterns,
            ),
        )

        queue: asyncio.Queue[Signal] = asyncio.Queue()

        tracker = None
        if settings.tracking.enabled:
            live_editor = LiveCardEditor(
                bot,
                on_permanent_failure=subscriptions.deactivate,
                on_message_gone=db.delete_thread,
            )

            website_price = None
            if settings.tracking.website_price_fallback_enabled:

                async def _website_fetch(url: str):
                    response = await http.get(url, follow_redirects=True)
                    return response.status_code, response.text

                website_price = WebsitePriceLookup(_website_fetch)

            tracker = LiveTracker(
                db,
                client,
                live_editor,
                opensea=opensea,
                explorer_url=settings.chain.explorer_url,
                explorer_token_path=settings.chain.explorer_token_path,
                marketplace_chain=settings.links.opensea_chain,
                refresh_interval_s=settings.tracking.refresh_interval_s,
                poll_interval_s=settings.tracking.poll_interval_s,
                max_duration_hours=settings.tracking.max_duration_hours,
                stale_after_checks=settings.tracking.stale_after_checks,
                check_delay_s=settings.tracking.check_delay_s,
                sales_cache_ttl_s=settings.tracking.sales_cache_ttl_s,
                website=website_price,
                website_price_cache_ttl_s=settings.tracking.website_price_cache_ttl_s,
                pricing=pricing,
                on_signal=queue.put,
                near_sellout_threshold=settings.tracking.near_sellout_threshold,
            )

        if settings.llm.enabled:
            llm = LocalLLM(
                base_url=settings.llm.base_url,
                model=settings.llm.model,
                api_key=settings.llm.api_key,
                timeout_s=settings.llm.timeout_s,
            )

        poster = None
        if settings.poster.enabled:
            poster = await _build_poster(settings, db, bot, owner_chat_id, llm)
            if poster is None:
                log.error(
                    "poster включён, но не настроен (нужны twitter.accounts_db,"
                    " заполненный twitter.post_as и установленный пакет twikit)"
                )

        async def draft_post(sig: Signal) -> None:
            if poster is not None:
                await poster.maybe_draft(sig)
                await poster.maybe_quote(sig)

        pipeline = Pipeline(
            db,
            notifier,
            min_score=settings.alerts.min_score,
            noise=NoiseFilter(
                similarity_threshold=settings.noise.similarity_threshold,
                text_window_hours=settings.noise.text_window_hours,
                topic_cooldown_minutes=settings.noise.topic_cooldown_minutes,
                onchain_topic_cooldown_minutes=settings.noise.onchain_topic_cooldown_minutes,
                max_alerts_per_hour=settings.noise.max_alerts_per_hour,
                min_author_followers=settings.noise.min_author_followers,
                urgent_score=settings.noise.urgent_score,
            ),
            quality=(
                SignalQualityFilter(llm)
                if llm is not None and settings.llm.filter_alerts
                else None
            ),
            on_delivered=tracker.track if tracker is not None else None,
            on_signal_delivered=draft_post if poster is not None else None,
        )

        async def persist_cursor() -> None:
            while True:
                await asyncio.sleep(CURSOR_SAVE_INTERVAL_S)
                await db.set_cursor("block", str(source.next_block))

        async def deliver_reminders() -> None:
            while True:
                await asyncio.sleep(REMINDER_TICK_S)
                try:
                    await reminders.deliver_due()
                except Exception:
                    log.exception("цикл напоминаний споткнулся")

        async def deliver_digest() -> None:
            last_sent = await db.get_cursor("digest:last_sent")
            now_utc = datetime.now(timezone.utc)
            since = digest_since(last_sent, now=now_utc)
            projects = await db.digest_projects_since(since)
            announcements = await db.digest_announcements_since(since)
            announcements = await attach_takes(announcements, llm)
            await db.set_cursor("digest:last_sent", now_utc.isoformat())

            text = format_digest(projects, announcements)
            if text is None:
                return
            for chat_id in await subscriptions.active_chat_ids():
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("не удалось отправить дайджест %s", chat_id)

        async def send_digest_loop() -> None:
            # Локальное время контейнера (переменная TZ), не UTC — чтобы
            # дайджест приходил утром/днём/вечером по часам владельца.
            digest_times = parse_times(settings.digest.times)
            while True:
                target = next_digest_at(datetime.now(), digest_times)
                await asyncio.sleep(max((target - datetime.now()).total_seconds(), 0))
                try:
                    await deliver_digest()
                except Exception:
                    log.exception("не удалось собрать дайджест")

        runners = [
            source.run(queue),
            pipeline.run(queue),
            persist_cursor(),
            deliver_reminders(),
        ]
        if settings.digest.enabled:
            runners.append(send_digest_loop())
        if tracker is not None:
            runners.append(tracker.run())

        host_updater = HostUpdater(Path(settings.db_path).parent)

        async def watch_host_updates() -> None:
            # Сразу при старте — на случай, если результат появился, пока
            # бот перезапускался (а он перезапустится ровно тогда: весь
            # смысл кнопки в пересборке контейнера). Дальше раз в 15 с —
            # запрос обычно обрабатывается за секунды, реже не нужно.
            while True:
                result = host_updater.poll_result()
                if result is not None:
                    try:
                        await bot.edit_message_text(
                            chat_id=int(result["chat_id"]),
                            message_id=int(result["message_id"]),
                            text=format_update_result(result),
                            parse_mode="HTML",
                        )
                    except Exception:
                        log.exception("не удалось показать результат обновления с GitHub")
                await asyncio.sleep(15)

        # Бот отвечает всем, кто ему напишет, — это отдельный цикл опроса.
        dispatcher = Dispatcher()
        dispatcher.include_router(
            build_router(
                subscriptions,
                db,
                reminders=reminders,
                tracker=tracker,
                poster=poster,
                twitter_enabled=settings.twitter.enabled,
                host_updater=host_updater,
                llm=llm,
                log_path=Path(settings.db_path).parent / "bot.log",
                # Только для локального файла: общую базу в Turso так не
                # выгрузить, да и незачем — там и лимит для этого кончился.
                db_path=Path(settings.db_path) if not settings.db_url else None,
            )
        )

        async def serve_commands() -> None:
            from aiogram.exceptions import TelegramConflictError

            try:
                await dispatcher.start_polling(bot, handle_signals=False)
            except TelegramConflictError as exc:
                raise BotAlreadyRunningError(str(exc)) from exc

        runners.append(serve_commands())
        runners.append(watch_host_updates())
        log.info("подписчиков сейчас: %d", await db.count_subscribers())

        twitter = _build_twitter(settings, db)
        if twitter is not None:
            log.info(
                "twitter: %d поисковых запросов, %d аккаунтов в watchlist",
                len(settings.twitter.queries),
                len(settings.twitter.watchlist),
            )
            runners.append(twitter.run(queue))

        try:
            await asyncio.gather(*runners)
        finally:
            # Сохраняем прогресс, чтобы после перезапуска не начинать с головы цепи.
            await db.set_cursor("block", str(source.next_block))
    finally:
        await bot.session.close()
        await client.aclose()
        await http.aclose()
        if llm is not None:
            await llm.aclose()
        await db.close()


def _build_twitter(settings: Settings, db: Database):
    if not settings.twitter.enabled:
        return None
    try:
        from twscrape import API
    except ImportError:
        log.error("twitter включён, но twscrape не установлен")
        return None

    return TwitterSource(
        API(settings.twitter.accounts_db),
        queries=settings.twitter.queries,
        watchlist=settings.twitter.watchlist,
        chain_name=settings.chain.name,
        search_interval_s=settings.twitter.search_interval_s,
        watchlist_interval_s=settings.twitter.watchlist_interval_s,
        per_query_limit=settings.twitter.per_query_limit,
        cursor_get=db.get_cursor,
        cursor_set=db.set_cursor,
    )


async def _build_poster(
    settings: Settings,
    db: Database,
    bot: Bot,
    owner_chat_id: int | None,
    llm: LocalLLM | None = None,
) -> Poster | None:
    """None при любой нехватке настройки — вызывающий код решает, шуметь ли об этом.

    Постящий аккаунт нарочно берётся тем же самым twscrape-аккаунтом, что
    уже авторизован на чтение (twitter.accounts_db + twitter.post_as):
    заводить для публикации отдельный логин незачем, раз оба действия —
    от имени одного и того же личного аккаунта.
    """
    if not settings.twitter.post_as:
        return None

    try:
        from twscrape import AccountsPool
    except ImportError:
        log.error("poster включён, но twscrape не установлен")
        return None

    try:
        import twikit  # noqa: F401  — только чтобы дать понятную ошибку, если пакета нет
    except ImportError:
        log.error("poster включён, но twikit не установлен")
        return None

    pool = AccountsPool(settings.twitter.accounts_db)
    try:
        account = await pool.get(settings.twitter.post_as)
    except ValueError:
        log.error(
            "poster: аккаунт %s не найден в %s",
            settings.twitter.post_as,
            settings.twitter.accounts_db,
        )
        return None

    if not account.has_session:
        log.error("poster: у аккаунта %s нет рабочей сессии (cookies)", account.username)
        return None

    async def notify_owner(post_id: int, text: str) -> None:
        if owner_chat_id is None:
            return
        from .notify.keyboards import post_draft_keyboard

        await bot.send_message(
            chat_id=owner_chat_id,
            text=f"🐦 <b>Черновик поста в X</b>\n\n{html.escape(text)}",
            parse_mode="HTML",
            reply_markup=post_draft_keyboard(post_id),
        )

    return Poster(
        db,
        twitter=TwikitPoster(account.cookies),
        notify_owner=notify_owner,
        marketplace_chain=settings.links.opensea_chain,
        max_posts_per_day=settings.poster.max_posts_per_day,
        max_quotes_per_day=settings.poster.max_quotes_per_day,
        llm=llm,
    )


def _build_opensea(settings: Settings, http: httpx.AsyncClient):
    if not settings.links.opensea_enabled:
        return None

    async def fetch(url: str, headers: dict[str, str]):
        response = await http.get(url, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    return OpenSeaClient(
        fetch,
        chain=settings.links.opensea_chain,
        api_key=settings.links.opensea_api_key,
    )


async def _resolve_start_block(db: Database, client, settings: Settings) -> int:
    cursor = await db.get_cursor("block")
    if cursor is not None:
        start = int(cursor)
        log.info("продолжаем с блока %d", start)
        return start

    head = int(await client.call("eth_blockNumber"), 16)
    start = max(head - settings.chain.confirmations, 0)
    log.info("первый запуск, начинаем с блока %d (голова цепи %d)", start, head)
    return start
