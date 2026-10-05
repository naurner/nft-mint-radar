"""Живая карточка коллекции: то же сообщение, но с актуальными цифрами.

В отличие от format_alert (одноразовое событие) эта карточка правится на
месте — цена, прогресс минта и флор обновляются в уже отправленном
сообщении вместо того, чтобы плодить новые.
"""

from __future__ import annotations

import asyncio
import html
import logging

from ..enrich.models import ProjectLinks
from ..models import format_amount as _amount

log = logging.getLogger(__name__)


def _link(url: str | None, label: str) -> str | None:
    if not url:
        return None
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def format_tracking_card(
    project: dict,
    info,
    links: ProjectLinks,
    *,
    floor: float | None,
    floor_symbol: str = "ETH",
    floor_usd: str | None = None,
    num_owners: int | None,
    now,
    sold_out: bool,
    usd_price: str | None = None,
    mint_price_eth_from_site: float | None = None,
    total_volume: float | None = None,
    volume_symbol: str = "ETH",
    volume_usd: str | None = None,
    total_sales: int | None = None,
    average_price: float | None = None,
    average_usd: str | None = None,
    last_sale: float | None = None,
    last_sale_symbol: str = "ETH",
    last_sale_usd: str | None = None,
    highest_sale: float | None = None,
    highest_sale_symbol: str = "ETH",
    highest_sale_usd: str | None = None,
) -> str:
    label = html.escape(project.get("name") or project.get("symbol") or project["address"])
    icon = "🔴" if sold_out else "🟢"
    lines = [f"{icon} <b>{label}</b>"]
    if sold_out:
        lines.append("Распродано")

    max_supply = info.max_supply or project.get("max_supply")
    if info.total_supply is not None and max_supply:
        pct = min(int(info.total_supply / max_supply * 100), 100)
        lines.append(f"📦 {info.total_supply} / {max_supply} ({pct}%)")
    elif info.total_supply is not None:
        lines.append(f"📦 сминчено: {info.total_supply}")

    price = info.mint_price_wei
    if price is None:
        price = project.get("mint_price_wei")

    eth = None
    source_note = ""
    if price is not None:
        eth = price / 1e18
    elif mint_price_eth_from_site is not None:
        # Контракт не выдал цену ни одним из угадываемых геттеров — это
        # значение снято эвристикой с сайта проекта, а не с блокчейна,
        # поэтому помечено отдельно и может ошибаться.
        eth = mint_price_eth_from_site
        source_note = " (с сайта)"

    if eth is not None:
        usd_suffix = f" (≈ {html.escape(usd_price)})" if usd_price else ""
        lines.append(
            f"💰 минт: бесплатно{source_note}{usd_suffix}"
            if eth == 0
            else f"💰 минт: {_amount(eth)} ETH{source_note}{usd_suffix}"
        )

    if floor is not None:
        suffix = f" (≈ {html.escape(floor_usd)})" if floor_usd else ""
        lines.append(f"📈 флор: {_amount(floor)} {html.escape(floor_symbol)}{suffix}")
    if num_owners is not None:
        lines.append(f"👥 держателей: {num_owners}")

    if total_volume is not None or total_sales is not None:
        parts = []
        if total_volume is not None:
            suffix = f" (≈ {html.escape(volume_usd)})" if volume_usd else ""
            parts.append(f"{_amount(total_volume)} {html.escape(volume_symbol)}{suffix}")
        if total_sales is not None:
            parts.append(f"{total_sales} продаж")
        lines.append("🔁 объём: " + ", ".join(parts))
    if average_price is not None:
        # Средняя цена продажи считается в той же валюте, что и объём.
        suffix = f" (≈ {html.escape(average_usd)})" if average_usd else ""
        lines.append(
            f"⚖️ средняя цена продажи: {_amount(average_price)} "
            f"{html.escape(volume_symbol)}{suffix}"
        )
    if last_sale is not None:
        suffix = f" (≈ {html.escape(last_sale_usd)})" if last_sale_usd else ""
        lines.append(
            f"🕐 последняя продажа: {_amount(last_sale)} "
            f"{html.escape(last_sale_symbol)}{suffix}"
        )
    if highest_sale is not None:
        suffix = f" (≈ {html.escape(highest_sale_usd)})" if highest_sale_usd else ""
        lines.append(
            f"🏆 макс. продажа (из последних): {_amount(highest_sale)} "
            f"{html.escape(highest_sale_symbol)}{suffix}"
        )

    lines.append("")
    lines.append(f"<code>{project['address']}</code>")

    where = [_link(links.explorer, "🔍 эксплорер"), _link(links.opensea, "🌊 OpenSea")]
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

    lines.append("")
    lines.append(f"обновлено {now.strftime('%H:%M UTC')}")
    return "\n".join(lines)


class LiveCardEditor:
    """Правит уже отправленное сообщение вместо отправки нового.

    Знает только про один чат/сообщение за раз — какие чаты нужно править
    и когда, решает LiveTracker; здесь только механика самой правки
    и разбор типовых ошибок Telegram.
    """

    def __init__(self, bot, *, on_permanent_failure=None, on_message_gone=None):
        self._bot = bot
        self._on_permanent_failure = on_permanent_failure
        self._on_message_gone = on_message_gone

    async def edit(self, chat_id: int, message_id: int, address: str, text: str) -> None:
        from .telegram import is_permanent_failure

        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            message = str(exc).lower()

            if "message is not modified" in message:
                # Цифры не изменились с прошлой проверки — это не ошибка.
                return

            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                await asyncio.sleep(float(retry_after))
                await self.edit(chat_id, message_id, address, text)
                return

            if (
                "message to edit not found" in message
                or "message can't be edited" in message
                or "message_id_invalid" in message
            ):
                # Человек удалил сообщение — обновлять больше нечего.
                if self._on_message_gone is not None:
                    await self._on_message_gone(chat_id, address)
                return

            if is_permanent_failure(exc) and self._on_permanent_failure is not None:
                await self._on_permanent_failure(chat_id)
                return

            log.exception("не удалось обновить карточку в чате %s", chat_id)
