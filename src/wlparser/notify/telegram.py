"""Рассылка алертов в Telegram. Один проект — одна ветка в каждом чате."""

from __future__ import annotations

import asyncio
import html
import logging

from ..enrich.models import ProjectLinks
from ..models import Signal

log = logging.getLogger(__name__)

_ICONS = {
    "contract_deployed": "🆕",
    "mint_started": "🚀",
    "mint_surge": "🔥",
    "mint_near_sellout": "⏳",
    "wl_announced": "🎟",
    "chatter": "💬",
}

_TITLES = {
    "contract_deployed": "Новый контракт",
    "mint_started": "Начался минт",
    "mint_surge": "Всплеск минта",
    "mint_near_sellout": "Почти распродано",
    "wl_announced": "Анонс WL",
    "chatter": "Упоминание",
}

DESCRIPTION_LIMIT = 180

# Признаки того, что чат недоступен навсегда: человек заблокировал бота,
# удалил аккаунт или удалил чат. Повторять отправку туда бессмысленно.
_PERMANENT_MARKERS = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
    "forbidden",
)

_MISSING_REPLY_MARKERS = (
    "message to be replied not found",
    "message to reply not found",
    "reply message not found",
)


def is_permanent_failure(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _PERMANENT_MARKERS)


def is_missing_reply(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _MISSING_REPLY_MARKERS)


def _link(url: str | None, label: str) -> str | None:
    if not url:
        return None
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _describe(text: str | None) -> str | None:
    """Описание пишет владелец коллекции — чужой текст, режем и экранируем."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > DESCRIPTION_LIMIT:
        collapsed = collapsed[:DESCRIPTION_LIMIT].rstrip() + "…"
    return html.escape(collapsed)


def format_alert(
    signal: Signal, links: ProjectLinks | None = None, *, usd_price: str | None = None
) -> str:
    """Собирает текст алерта.

    Всё, что пришло из соцсетей и от владельцев коллекций, экранируется:
    чужой текст не должен уметь подставлять разметку в наше сообщение.
    """
    links = links or ProjectLinks()
    icon = _ICONS.get(signal.kind, "•")
    title = _TITLES.get(signal.kind, signal.kind.replace("_", " ").title())

    lines = [f"{icon} <b>{html.escape(title)}</b>", html.escape(signal.text)]

    if usd_price:
        lines.append(f"💵 ≈ {html.escape(usd_price)}")

    description = _describe(links.description)
    if description:
        lines.append("")
        lines.append(f"<i>{description}</i>")

    if signal.author and signal.source != "onchain":
        lines.append(f"от: {html.escape(str(signal.author))}")

    if signal.contract_address:
        lines.append("")
        lines.append(f"<code>{signal.contract_address}</code>")

    where = [
        _link(links.explorer, "🔍 эксплорер"),
        _link(links.opensea, "🌊 OpenSea"),
    ]
    where = [item for item in where if item]
    if where:
        lines.append(" · ".join(where))

    socials = [
        _link(links.twitter, "🐦 Twitter"),
        _link(links.discord, "💬 Discord"),
        _link(links.telegram, "✈️ Telegram"),
        _link(links.website, "🌐 Сайт"),
    ]
    socials = [item for item in socials if item]
    if socials:
        lines.append(" · ".join(socials))

    if signal.url:
        lines.append(html.escape(signal.url))

    lines.append("")
    lines.append(signal.timestamp.strftime("%Y-%m-%d %H:%M UTC"))
    return "\n".join(lines)


class TelegramBroadcaster:
    """Рассылает алерт всем подписчикам, ведя отдельную ветку в каждом чате."""

    def __init__(
        self,
        bot,
        *,
        recipients,
        thread_lookup,
        thread_store,
        on_permanent_failure=None,
        enricher=None,
        owner_chat_id: int | None = None,
        owner_head_start_s: float = 0.0,
        send_delay_s: float = 0.05,
        keyboard_factory=None,
        pricing=None,
    ):
        self._bot = bot
        self._recipients = recipients
        self._thread_lookup = thread_lookup
        self._thread_store = thread_store
        self._on_permanent_failure = on_permanent_failure
        self._enricher = enricher
        self._owner_chat_id = int(owner_chat_id) if owner_chat_id else None
        self._owner_head_start = owner_head_start_s
        self._send_delay = send_delay_s
        # Кнопка «Напомнить» под каждым алертом.
        self._keyboard_factory = keyboard_factory
        # Перевод цены минта в доллары — необязателен, без него просто нет строки.
        self._pricing = pricing

    async def send(self, signal: Signal) -> None:
        links = ProjectLinks()
        if self._enricher is not None:
            links = await self._enricher.enrich(signal.contract_address)

        usd_price = None
        if self._pricing is not None:
            mint_price_wei = (signal.raw or {}).get("info", {}).get("mint_price_wei")
            if mint_price_wei is not None:
                usd_price = await self._pricing.usd_line(mint_price_wei)

        text = format_alert(signal, links, usd_price=usd_price)

        chat_ids = await self._recipients()
        if not chat_ids:
            log.warning("нет ни одного подписчика, алерт никому не ушёл")
            return

        # Владелец получает сообщение первым. Фора настраивается: при
        # публичном боте она решает, кто увидит находку раньше остальных.
        ordered = sorted(chat_ids, key=lambda cid: cid != self._owner_chat_id)
        for index, chat_id in enumerate(ordered):
            if (
                index == 1
                and self._owner_head_start
                and ordered[0] == self._owner_chat_id
            ):
                await asyncio.sleep(self._owner_head_start)
            await self._send_one(chat_id, signal, text)
            if self._send_delay:
                await asyncio.sleep(self._send_delay)

    async def _send_one(self, chat_id: int, signal: Signal, text: str) -> None:
        address = signal.contract_address
        existing = self._thread_lookup(chat_id, address) if address else None

        message = await self._deliver(chat_id, text, existing)
        if message is None:
            return
        if address and not existing:
            await self._thread_store(chat_id, address, message.message_id)

    async def _deliver(self, chat_id: int, text: str, reply_to: int | None):
        kwargs = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        if self._keyboard_factory is not None:
            kwargs["reply_markup"] = self._keyboard_factory()

        try:
            return await self._bot.send_message(**kwargs)
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            # Ноль — валидное значение «повтори сразу», проверяем на None.
            if retry_after is not None:
                await asyncio.sleep(float(retry_after))
                return await self._deliver(chat_id, text, reply_to)

            if reply_to and is_missing_reply(exc):
                # Человек удалил исходное сообщение ветки — шлём заново
                # как самостоятельное, чтобы алерт не потерялся.
                return await self._deliver(chat_id, text, None)

            if is_permanent_failure(exc) and self._on_permanent_failure is not None:
                log.info("чат %s недоступен, отключаю рассылку туда", chat_id)
                await self._on_permanent_failure(chat_id)
                return None

            log.exception("не удалось отправить алерт в чат %s", chat_id)
            return None
