"""Кнопки под сообщениями.

Telegram ограничивает callback_data 64 байтами, поэтому коды короткие,
а всё остальное берётся из самого сообщения или из базы.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .reminders import PRESETS

# Префиксы callback_data
CB_MENU = "r:menu"
CB_PICK = "r:p:"  # + код пресета
CB_BACK = "r:back"
CB_NOTE = "r:n:"  # + id напоминания
CB_FINAL = "r:f:"  # + id напоминания
CB_POST_YES = "p:y:"  # + id черновика поста в X
CB_POST_NO = "p:n:"  # + id черновика поста в X
CB_SYS_STATUS = "sys:status"
CB_SYS_RECENT = "sys:recent"
CB_SYS_TOP = "sys:top"
CB_SYS_UPDATE = "sys:update"
CB_SYS_HELP = "sys:help"
CB_SYS_LOGS = "sys:logs"
CB_SYS_DBDUMP = "sys:dbdump"
CB_SYS_TESTPOST = "sys:testpost"
CB_SYS_CLEAR_DRAFTS = "sys:cleardrafts"

# Текст кнопки постоянной клавиатуры — Telegram шлёт его как обычное
# текстовое сообщение по нажатию, поэтому это же значение служит и
# кнопкой, и текстом, который потом ловит обработчик.
MENU_BUTTON_TEXT = "☰ Меню"


def persistent_menu_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка снизу экрана, всегда на виду — не нужно помнить команду /menu."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def alert_keyboard() -> InlineKeyboardMarkup:
    """Одна кнопка под каждым алертом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Напомнить", callback_data=CB_MENU)]
        ]
    )


def time_menu_keyboard() -> InlineKeyboardMarkup:
    """Выбор времени. По две кнопки в ряд, чтобы попадать пальцем."""
    rows = []
    buttons = [
        InlineKeyboardButton(text=preset.label, callback_data=CB_PICK + preset.code)
        for preset in PRESETS
    ]
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append([InlineKeyboardButton(text="← Отмена", callback_data=CB_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def note_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Добавить заметку",
                    callback_data=CB_NOTE + str(reminder_id),
                )
            ]
        ]
    )


def post_draft_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Под черновиком поста в X — публикация только по явному нажатию."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать", callback_data=CB_POST_YES + str(post_id)
                ),
                InlineKeyboardButton(
                    text="❌ Пропустить", callback_data=CB_POST_NO + str(post_id)
                ),
            ]
        ]
    )


def main_menu_keyboard(
    *,
    show_update: bool = True,
    show_owner_tools: bool = False,
    show_poster_tools: bool = False,
    show_db_dump: bool = False,
) -> InlineKeyboardMarkup:
    """Общее меню — /menu и кнопка того же назначения под ним самим.

    "Обновить с GitHub" скрывается, если host_updater не передан в
    build_router, чтобы не предлагать кнопку, которая ничего не сможет
    сделать. Ряд с "Логи"/"Тестовый пост"/"Очистить черновики" — только
    владельцу: это либо диагностика, либо действие с реальными
    последствиями (настоящий твит, списанные черновики), и остальным
    подписчикам, если они есть, показывать это незачем. "База (SQL)" —
    только если сейчас локальный файл (db_path), а не общая база в Turso.
    """
    rows = [
        [InlineKeyboardButton(text="📊 Статус", callback_data=CB_SYS_STATUS)],
        [
            InlineKeyboardButton(text="📋 Последние находки", callback_data=CB_SYS_RECENT),
            InlineKeyboardButton(text="🏆 Топ проектов", callback_data=CB_SYS_TOP),
        ],
        [InlineKeyboardButton(text="❓ Помощь", callback_data=CB_SYS_HELP)],
    ]
    if show_update:
        rows.append(
            [InlineKeyboardButton(text="🔄 Обновить с GitHub", callback_data=CB_SYS_UPDATE)]
        )
    if show_owner_tools:
        tools_row = [InlineKeyboardButton(text="📄 Логи", callback_data=CB_SYS_LOGS)]
        if show_db_dump:
            tools_row.append(
                InlineKeyboardButton(text="🗄 База (SQL)", callback_data=CB_SYS_DBDUMP)
            )
        rows.append(tools_row)
    if show_owner_tools and show_poster_tools:
        rows.append(
            [
                InlineKeyboardButton(text="🐦 Тестовый пост", callback_data=CB_SYS_TESTPOST),
                InlineKeyboardButton(
                    text="🧹 Очистить черновики", callback_data=CB_SYS_CLEAR_DRAFTS
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Кнопка на самом напоминании: повторить перед стартом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Ещё раз за минуту до",
                    callback_data=CB_FINAL + str(reminder_id),
                )
            ]
        ]
    )
