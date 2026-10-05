"""Команды бота: подписка, отписка, справка, последние находки.

Логика вынесена в SubscriptionService и не зависит от aiogram — это
позволяет проверить её тестами без запуска бота и сети.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from ..enrich.models import marketplace_url, safe_url

log = logging.getLogger(__name__)

WELCOME = (
    "👋 <b>Привет!</b>\n\n"
    "Я слежу за сетью Robinhood Chain и присылаю сообщение, когда появляется "
    "новая NFT-коллекция, начинается минт или в твиттере анонсируют WL.\n\n"
    "В каждом сообщении будут ссылки на контракт, OpenSea и соцсети проекта.\n\n"
    "Ты подписан. Команды:\n"
    "/menu — меню кнопками\n"
    "/recent — последние находки\n"
    "/stop — отписаться\n"
    "/help — справка"
)

WELCOME_BACK = "Готово, снова подписан. Отписаться — /stop"

GOODBYE = (
    "Отписал. Больше уведомлений не будет.\n"
    "Захочешь вернуться — просто отправь /start"
)

HELP = (
    "<b>Что я умею</b>\n\n"
    "🆕 новая NFT-коллекция задеплоена в сети\n"
    "🚀 минт пошёл — коллекцию берут разные кошельки\n"
    "🔥 массово разбирают прямо сейчас\n"
    "🎟 в твиттере объявили WL или старт минта\n\n"
    "Все сообщения по одному проекту идут одной веткой — ответами "
    "на первое сообщение о нём. Первое сообщение потом само обновляется: "
    "цена минта, сколько сминчено и флор на вторичке — без новых сообщений.\n\n"
    "/menu — меню кнопками (статус, находки, обновление и всё остальное)\n"
    "/recent — последние находки\n"
    "/top — какие из отслеживаемых выглядят интереснее\n"
    "/find название — вся информация по конкретному проекту\n"
    "/stop — отписаться"
)


class SubscriptionService:
    """Подписки и уведомление владельца о новых людях."""

    def __init__(self, db, *, owner_chat_id: int | None = None, notify_owner=None):
        self._db = db
        self._owner_chat_id = int(owner_chat_id) if owner_chat_id else None
        self._notify_owner = notify_owner

    async def ensure_owner(self) -> None:
        """Владелец подписан всегда, даже если он ни разу не писал боту."""
        if self._owner_chat_id is None:
            return
        await self._db.add_subscriber(
            self._owner_chat_id, display_name="владелец", is_owner=True
        )

    async def subscribe(
        self, chat_id: int, *, username: str | None, display_name: str | None
    ) -> str:
        is_owner = self._owner_chat_id is not None and int(chat_id) == self._owner_chat_id
        is_new = await self._db.add_subscriber(
            chat_id, username=username, display_name=display_name, is_owner=is_owner
        )

        if is_new and not is_owner:
            await self._announce_newcomer(chat_id, username, display_name)

        return WELCOME if is_new else WELCOME_BACK

    async def unsubscribe(self, chat_id: int) -> str:
        await self._db.set_subscriber_active(chat_id, False)
        return GOODBYE

    async def active_chat_ids(self) -> list[int]:
        return [row["chat_id"] for row in await self._db.list_subscribers()]

    async def deactivate(self, chat_id: int) -> None:
        await self._db.set_subscriber_active(chat_id, False)

    def is_owner(self, chat_id: int) -> bool:
        return self._owner_chat_id is not None and int(chat_id) == self._owner_chat_id

    async def _announce_newcomer(
        self, chat_id: int, username: str | None, display_name: str | None
    ) -> None:
        """Владельцу — сообщение о каждом новом человеке."""
        if self._notify_owner is None:
            return

        total = await self._db.count_subscribers()
        who = html.escape(display_name or "без имени")
        handle = f"@{html.escape(username)}" if username else "без юзернейма"
        text = (
            f"👤 <b>Новый подписчик</b>\n"
            f"{who} · {handle}\n"
            f"<code>{chat_id}</code>\n\n"
            f"Всего подписчиков: {total}"
        )
        try:
            await self._notify_owner(text)
        except Exception:
            log.exception("не удалось сообщить владельцу о новом подписчике")


def format_recent(rows: list[dict], limit: int) -> str:
    if not rows:
        return "Пока ничего не найдено."

    lines = [f"<b>Последние находки</b> (до {limit})", ""]
    for row in rows:
        when = (row.get("ts") or "")[:16].replace("T", " ")
        kind = _ICON_BY_KIND.get(row.get("kind"), "•")
        text = html.escape((row.get("text") or "")[:90])
        line = f"{kind} <code>{when}</code> {text}"
        # Твиттерная находка — ссылка на твит; он-чейн — на OpenSea по
        # адресу контракта. То же самое, что уже есть в обычных алертах,
        # только в списке /recent их до сих пор не было видно.
        link = safe_url(row.get("url")) or marketplace_url(row.get("chain"), row.get("address"))
        if link:
            line += f' — <a href="{html.escape(link)}">ссылка</a>'
        lines.append(line)
    return "\n".join(lines)


_ICON_BY_KIND = {
    "contract_deployed": "🆕",
    "mint_started": "🚀",
    "mint_surge": "🔥",
    "mint_near_sellout": "⏳",
    "wl_announced": "🎟",
    "chatter": "💬",
}


def format_top(rows: list[dict]) -> str:
    """Эвристический рейтинг, не решение купить. Объяснение — как считалось."""
    if not rows:
        return (
            "Пока нечего показать. Оценка появляется через несколько "
            "проверок после первого алерта по проекту."
        )

    lines = ["<b>Что выглядит интереснее</b> (эвристика, не совет)", ""]
    for row in rows:
        label = html.escape(row.get("name") or row.get("symbol") or row["address"])
        score = row.get("interest_score")
        reason = html.escape(row.get("interest_reason") or "")
        lines.append(f"<b>{score}</b> — {label}")
        if reason:
            lines.append(f"   {reason}")
    return "\n".join(lines)


async def build_status_text(
    db,
    *,
    tracking_enabled: bool,
    poster_enabled: bool,
    twitter_enabled: bool,
    llm=None,
) -> str:
    """Живой снимок состояния — что сейчас реально включено и чем занято.

    Отдельно считает черновики постов и уже опубликованное: жалоба "автопост
    ничего не предложил" чаще всего означает "posted=0, pending=0" — то есть
    подходящих сигналов ещё не было, а не что что-то сломано — и эта строка
    отвечает на вопрос сама, без переписки.

    Коммит показывается тем же способом, что и в самом ответе на кнопку
    "Обновить с GitHub" — mac-autopull.sh печёт его в образ (GIT_COMMIT),
    так что тут видно, обновился ли Мак на самом деле, без захода по SSH.
    """
    import os
    from datetime import datetime, timedelta, timezone

    subscribers = await db.count_subscribers()
    active_projects = len(await db.active_tracked_projects())
    block = await db.get_cursor("block")
    commit = os.environ.get("GIT_COMMIT", "unknown")

    lines = ["<b>📊 Статус</b>", ""]
    if commit and commit != "unknown":
        lines.append(f"🏷 коммит: <code>{html.escape(commit)}</code>")
    lines.append(f"👥 подписчиков: {subscribers}")
    lines.append(f"👀 отслеживается сейчас: {active_projects}")
    if block:
        lines.append(f"⛓ блок: {html.escape(str(block))}")
    lines.append(f"📈 живое отслеживание: {'включено' if tracking_enabled else 'выключено'}")
    lines.append(f"🐦 твиттер-источник: {'включён' if twitter_enabled else 'выключен'}")

    autopost_line = f"📮 автопост в X: {'включён' if poster_enabled else 'выключен'}"
    if poster_enabled:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        posted = await db.count_posts_since(since)
        quoted = await db.count_posts_since(since, kind="quote")
        pending = await db.count_pending_posts()
        drafted = await db.count_drafts_since(since)
        autopost_line += (
            f" — предложено за сутки: {drafted}, ждёт решения: {pending}, "
            f"опубликовано: {posted}, процитировано: {quoted}"
        )
    lines.append(autopost_line)

    if llm is None:
        lines.append("🧠 локальный ИИ: выключен в настройках")
    else:
        ok, detail = await llm.health()
        lines.append(f"🧠 локальный ИИ: {'✅' if ok else '⚠️'} {html.escape(detail)}")

    return "\n".join(lines)


def build_router(
    service: SubscriptionService,
    db,
    *,
    recent_limit: int = 10,
    reminders=None,
    tracker=None,
    poster=None,
    twitter_enabled: bool = False,
    host_updater=None,
    llm=None,
    log_path=None,
    db_path=None,
):
    """Собирает aiogram-роутер поверх сервисов.

    Импорт aiogram внутри функции: тесты проверяют SubscriptionService
    напрямую и не должны зависеть от того, установлен ли фреймворк.
    """
    from aiogram import F, Router
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import BufferedInputFile, CallbackQuery, Message

    from .keyboards import (
        CB_BACK,
        CB_FINAL,
        CB_MENU,
        CB_NOTE,
        CB_PICK,
        CB_POST_NO,
        CB_POST_YES,
        CB_SYS_CLEAR_DRAFTS,
        CB_SYS_DBDUMP,
        CB_SYS_HELP,
        CB_SYS_LOGS,
        CB_SYS_RECENT,
        CB_SYS_STATUS,
        CB_SYS_TESTPOST,
        CB_SYS_TOP,
        CB_SYS_UPDATE,
        MENU_BUTTON_TEXT,
        alert_keyboard,
        main_menu_keyboard,
        note_keyboard,
        persistent_menu_keyboard,
        time_menu_keyboard,
    )
    from .logs import read_log
    from .reminders import PRESET_BY_CODE, format_confirmation, format_local

    router = Router()

    async def _send_log(target) -> None:
        """target — Message или CallbackQuery.message: у обоих есть .answer/.answer_document."""
        text = read_log(log_path, search=None) if log_path is not None else ""
        if not text:
            await target.answer("Лог пуст.")
            return
        if len(text) <= 3500:
            await target.answer(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")
        else:
            data = text.encode("utf-8")
            await target.answer_document(
                BufferedInputFile(data, filename="bot.log"),
                caption="Хвост лога — не поместился в сообщение.",
            )

    if reminders is not None:

        @router.callback_query(F.data == CB_MENU)
        async def on_remind(callback: CallbackQuery) -> None:
            await callback.message.edit_reply_markup(reply_markup=time_menu_keyboard())
            await callback.answer("Через сколько напомнить?")

        @router.callback_query(F.data == CB_BACK)
        async def on_back(callback: CallbackQuery) -> None:
            await callback.message.edit_reply_markup(reply_markup=alert_keyboard())
            await callback.answer()

        @router.callback_query(F.data.startswith(CB_PICK))
        async def on_pick(callback: CallbackQuery) -> None:
            preset = PRESET_BY_CODE.get(callback.data[len(CB_PICK) :])
            if preset is None:
                await callback.answer("Не понял время")
                return

            # Текст берётся из самого сообщения: так напоминание повторит
            # исходный алерт дословно, включая ссылки.
            payload = callback.message.html_text or callback.message.text or ""
            reminder_id, due, target = await reminders.schedule(
                chat_id=callback.message.chat.id,
                payload=payload,
                minutes=preset.minutes,
            )
            await callback.message.edit_reply_markup(reply_markup=alert_keyboard())
            await callback.message.answer(
                format_confirmation(target, due, None),
                parse_mode="HTML",
                reply_markup=note_keyboard(reminder_id),
            )
            await callback.answer(f"Напомню за 10 минут до {format_local(target)}")

        @router.callback_query(F.data.startswith(CB_NOTE))
        async def on_note(callback: CallbackQuery) -> None:
            reminder_id = int(callback.data[len(CB_NOTE) :])
            reminders.expect_note(callback.message.chat.id, reminder_id)
            await callback.message.answer(
                "📝 Напиши заметку следующим сообщением — приложу её к напоминанию."
            )
            await callback.answer()

        @router.callback_query(F.data.startswith(CB_FINAL))
        async def on_final(callback: CallbackQuery) -> None:
            reminder_id = int(callback.data[len(CB_FINAL) :])
            due = await reminders.schedule_final(reminder_id)
            if due is None:
                await callback.answer("Это напоминание уже не найти")
                return
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer(f"Повторю в {format_local(due)}")

        @router.message(F.text, lambda m: reminders.is_awaiting_note(m.chat.id))
        async def on_note_text(message: Message) -> None:
            # Стоит выше общего обработчика: пока ждём заметку, обычный текст
            # не должен уходить в подписку.
            if await reminders.attach_note(message.chat.id, message.text):
                await message.answer("📝 Заметка сохранена.")

    if poster is not None:

        @router.callback_query(F.data.startswith(CB_POST_YES))
        async def on_post_yes(callback: CallbackQuery) -> None:
            post_id = int(callback.data[len(CB_POST_YES) :])
            error = await poster.approve(post_id)
            await callback.message.edit_reply_markup(reply_markup=None)
            if error:
                await callback.answer(error, show_alert=True)
                return
            post = await db.get_post(post_id)
            url = post.get("tweet_url") if post else None
            await callback.message.answer(
                f"Опубликовано: {html.escape(url)}" if url else "Опубликовано."
            )
            await callback.answer("Опубликовано")

        @router.callback_query(F.data.startswith(CB_POST_NO))
        async def on_post_no(callback: CallbackQuery) -> None:
            post_id = int(callback.data[len(CB_POST_NO) :])
            await poster.skip(post_id)
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Пропущено")

        @router.message(Command("testpost"))
        async def on_testpost(message: Message, command: CommandObject) -> None:
            # Только владелец: черновик отсюда публикуется по одной кнопке
            # без проверки сигналом, и это не должно быть доступно кому-то,
            # кто просто написал боту /start.
            if not service.is_owner(message.chat.id):
                return
            text = (command.args or "").strip() or None
            await poster.send_test_draft(text)
            await message.answer("Черновик отправлен выше — нажми «Опубликовать».")

        @router.callback_query(F.data == CB_SYS_TESTPOST)
        async def on_menu_testpost(callback: CallbackQuery) -> None:
            if not service.is_owner(callback.message.chat.id):
                await callback.answer()
                return
            await poster.send_test_draft()
            await callback.message.answer("Черновик отправлен выше — нажми «Опубликовать».")
            await callback.answer()

        @router.callback_query(F.data == CB_SYS_CLEAR_DRAFTS)
        async def on_menu_clear_drafts(callback: CallbackQuery) -> None:
            if not service.is_owner(callback.message.chat.id):
                await callback.answer()
                return
            n = await db.clear_pending_posts()
            await callback.message.answer(
                f"Списано черновиков: {n}." if n else "Черновиков в очереди не было."
            )
            await callback.answer()

    if log_path is not None:

        @router.message(Command("logs"))
        async def on_logs(message: Message, command: CommandObject) -> None:
            if not service.is_owner(message.chat.id):
                return
            search = (command.args or "").strip() or None
            if search is None:
                await _send_log(message)
                return
            text = read_log(log_path, search=search)
            if not text:
                await message.answer(f"Ничего не найдено по «{html.escape(search)}».")
            elif len(text) <= 3500:
                await message.answer(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")
            else:
                data = text.encode("utf-8")
                await message.answer_document(
                    BufferedInputFile(data, filename="bot.log"),
                    caption="Совпадений много — во вложении.",
                )

        @router.callback_query(F.data == CB_SYS_LOGS)
        async def on_menu_logs(callback: CallbackQuery) -> None:
            if not service.is_owner(callback.message.chat.id):
                await callback.answer()
                return
            await _send_log(callback.message)
            await callback.answer()

    if db_path is not None:

        @router.message(Command("dbdump"))
        async def on_dbdump(message: Message) -> None:
            # Только владелец: в базе живут chat_id всех подписчиков —
            # не то, что стоит присылать кому попало по команде.
            if not service.is_owner(message.chat.id):
                return
            path = Path(db_path)
            if not path.exists():
                await message.answer("Файла базы пока нет.")
                return
            data = path.read_bytes()
            await message.answer_document(
                BufferedInputFile(data, filename=path.name),
                caption="Файл базы (SQLite) — целиком, как на диске.",
            )

        @router.callback_query(F.data == CB_SYS_DBDUMP)
        async def on_menu_dbdump(callback: CallbackQuery) -> None:
            if not service.is_owner(callback.message.chat.id):
                await callback.answer()
                return
            await on_dbdump(callback.message)
            await callback.answer()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        text = await service.subscribe(
            message.chat.id,
            username=getattr(user, "username", None),
            display_name=getattr(user, "full_name", None),
        )
        await message.answer(text, parse_mode="HTML", reply_markup=persistent_menu_keyboard())

    @router.message(Command("stop"))
    async def on_stop(message: Message) -> None:
        await message.answer(await service.unsubscribe(message.chat.id))

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        await message.answer(HELP, parse_mode="HTML")

    @router.message(Command("recent"))
    async def on_recent(message: Message) -> None:
        rows = await db.recent_signals(recent_limit)
        await message.answer(
            format_recent(rows, recent_limit),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("top"))
    async def on_top(message: Message) -> None:
        rows = await db.top_projects(10)
        await message.answer(
            format_top(rows), parse_mode="HTML", disable_web_page_preview=True
        )

    @router.message(Command("menu"))
    async def on_menu(message: Message) -> None:
        await message.answer(
            "<b>Меню</b>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(
                show_update=host_updater is not None,
                show_owner_tools=service.is_owner(message.chat.id),
                show_poster_tools=poster is not None,
                show_db_dump=db_path is not None,
            ),
        )

    @router.message(F.text == MENU_BUTTON_TEXT)
    async def on_menu_button(message: Message) -> None:
        # Кнопка постоянной клавиатуры внизу экрана шлёт свой текст как
        # обычное сообщение — этот обработчик и команда /menu делают одно
        # и то же, просто с двух разных входов.
        await on_menu(message)

    @router.callback_query(F.data == CB_SYS_STATUS)
    async def on_menu_status(callback: CallbackQuery) -> None:
        text = await build_status_text(
            db,
            tracking_enabled=tracker is not None,
            poster_enabled=poster is not None,
            twitter_enabled=twitter_enabled,
            llm=llm,
        )
        await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()

    @router.callback_query(F.data == CB_SYS_RECENT)
    async def on_menu_recent(callback: CallbackQuery) -> None:
        rows = await db.recent_signals(recent_limit)
        await callback.message.answer(
            format_recent(rows, recent_limit),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await callback.answer()

    @router.callback_query(F.data == CB_SYS_TOP)
    async def on_menu_top(callback: CallbackQuery) -> None:
        rows = await db.top_projects(10)
        await callback.message.answer(
            format_top(rows), parse_mode="HTML", disable_web_page_preview=True
        )
        await callback.answer()

    @router.callback_query(F.data == CB_SYS_HELP)
    async def on_menu_help(callback: CallbackQuery) -> None:
        await callback.message.answer(HELP, parse_mode="HTML")
        await callback.answer()

    @router.callback_query(F.data == CB_SYS_UPDATE)
    async def on_menu_update(callback: CallbackQuery) -> None:
        if host_updater is None:
            await callback.answer("Обновление по кнопке не настроено", show_alert=True)
            return
        if host_updater.has_pending():
            await callback.answer(
                "Обновление уже идёт — дождись результата в чате.", show_alert=True
            )
            return

        sent = await callback.message.answer("⏳ Прошу Мак обновиться с GitHub...")
        try:
            host_updater.request(chat_id=sent.chat.id, message_id=sent.message_id)
        except Exception:
            log.exception("не удалось поставить флаг на обновление")
            await sent.edit_text("Не получилось — смотри лог на сервере.")
            await callback.answer()
            return
        await callback.answer("Обновляюсь...")

    @router.message(Command("find"))
    async def on_find(message: Message, command: CommandObject) -> None:
        query = (command.args or "").strip()
        if not query:
            await message.answer(
                "Напиши название после команды, например:\n<code>/find cash bears</code>",
                parse_mode="HTML",
            )
            return
        if tracker is None:
            await message.answer("Слежение выключено в настройках — поиск недоступен.")
            return

        rows = await db.search_projects(query, limit=3)
        if not rows:
            await message.answer(
                f"Ничего не нашёл среди отслеживаемых проектов по «{html.escape(query)}»."
            )
            return

        for row in rows:
            try:
                text = await tracker.render_card(row, recent_sales=True)
            except Exception:
                log.exception("не удалось собрать карточку для %s", row["address"])
                continue
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    @router.message()
    async def on_anything_else(message: Message) -> None:
        # Любое сообщение боту — тоже заявка на подписку: человек нашёл бота
        # и написал, заставлять его угадывать команду незачем.
        text = await service.subscribe(
            message.chat.id,
            username=getattr(message.from_user, "username", None),
            display_name=getattr(message.from_user, "full_name", None),
        )
        await message.answer(text, parse_mode="HTML", reply_markup=persistent_menu_keyboard())

    return router
