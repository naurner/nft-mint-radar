"""Напоминания под алертами.

Замысел: минт начнётся не сейчас, а через час — под сообщением есть кнопка,
которой ставится напоминание. Оно приходит заранее, целиком повторяя
исходный алерт, чтобы не искать его в переписке. К напоминанию можно
приписать заметку, а на самом напоминании есть кнопка повторить ещё раз
за минуту до события — на случай, когда до старта ещё есть дела.

Расчёт времени и сборка текстов вынесены в чистые функции: их логику
нужно проверять тестами, а не глазами через Telegram.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Насколько раньше события приходит напоминание.
DEFAULT_LEAD_MINUTES = 10

# Повтор перед самым стартом.
FINAL_LEAD_MINUTES = 1

# Напоминание не должно прийти в ту же секунду, что и нажатие кнопки.
MIN_DELAY = timedelta(seconds=30)


@dataclass(frozen=True)
class Preset:
    code: str
    minutes: int
    label: str


PRESETS: tuple[Preset, ...] = (
    Preset("15m", 15, "15 мин"),
    Preset("30m", 30, "30 мин"),
    Preset("1h", 60, "1 час"),
    Preset("2h", 120, "2 часа"),
    Preset("4h", 240, "4 часа"),
    Preset("8h", 480, "8 часов"),
    Preset("12h", 720, "12 часов"),
    Preset("24h", 1440, "сутки"),
)

PRESET_BY_CODE = {preset.code: preset for preset in PRESETS}


def compute_due(
    target: datetime, *, lead_minutes: int, now: datetime
) -> datetime:
    """Когда отправить напоминание при событии в момент target.

    Если до события меньше, чем время опережения, напоминание всё равно
    придёт — просто почти сразу. Молча терять его нельзя: человек нажал
    кнопку и ждёт сообщения.
    """
    due = target - timedelta(minutes=lead_minutes)
    earliest = now + MIN_DELAY
    return max(due, earliest)


def humanize(delta: timedelta) -> str:
    """«через 2 ч 30 мин» человеческими словами."""
    total = max(int(delta.total_seconds()), 0)
    hours, minutes = divmod(total // 60, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def format_local(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def format_confirmation(target: datetime, due: datetime, now: datetime) -> str:
    lead = humanize(target - due)
    return (
        f"⏰ Напомню в <b>{format_local(due)}</b> "
        f"(за {lead} до {format_local(target)}).\n"
        "Можно приписать заметку — кнопкой ниже."
    )


def format_reminder(
    payload: str, *, note: str | None, target: datetime, stage: str
) -> str:
    """Текст напоминания: шапка, заметка и полная копия исходного алерта."""
    if stage == "final":
        head = f"⏰ <b>Через минуту</b> — {format_local(target)}"
    else:
        head = f"⏰ <b>Напоминание</b> — событие в {format_local(target)}"

    parts = [head]
    if note:
        parts.append(f"📝 <i>{html.escape(note)}</i>")
    parts.append("")
    parts.append(payload)
    return "\n".join(parts)


class ReminderService:
    """Хранение напоминаний и их отправка в срок."""

    def __init__(
        self,
        db,
        send,
        *,
        lead_minutes: int = DEFAULT_LEAD_MINUTES,
        now=lambda: datetime.now(timezone.utc),
    ):
        self._db = db
        self._send = send
        self._lead = lead_minutes
        self._now = now
        # Чат -> напоминание, к которому ждём текст заметки.
        self._awaiting_note: dict[int, int] = {}

    # --- создание ---

    async def schedule(
        self, *, chat_id: int, payload: str, minutes: int, address: str | None = None
    ) -> tuple[int, datetime, datetime]:
        now = self._now()
        target = now + timedelta(minutes=minutes)
        due = compute_due(target, lead_minutes=self._lead, now=now)
        reminder_id = await self._db.add_reminder(
            chat_id=chat_id,
            payload=payload,
            due_at=due,
            target_at=target,
            address=address,
            stage="lead",
        )
        return reminder_id, due, target

    async def schedule_final(self, reminder_id: int) -> datetime | None:
        """Повтор за минуту до события — кнопка на самом напоминании."""
        row = await self._db.get_reminder(reminder_id)
        if row is None:
            return None

        now = self._now()
        target = _parse(row["target_at"])
        due = compute_due(target, lead_minutes=FINAL_LEAD_MINUTES, now=now)
        await self._db.add_reminder(
            chat_id=row["chat_id"],
            payload=row["payload"],
            due_at=due,
            target_at=target,
            address=row["address"],
            note=row["note"],
            stage="final",
        )
        return due

    # --- заметка ---

    def expect_note(self, chat_id: int, reminder_id: int) -> None:
        self._awaiting_note[int(chat_id)] = int(reminder_id)

    def is_awaiting_note(self, chat_id: int) -> bool:
        return int(chat_id) in self._awaiting_note

    async def attach_note(self, chat_id: int, text: str) -> bool:
        reminder_id = self._awaiting_note.pop(int(chat_id), None)
        if reminder_id is None:
            return False
        await self._db.set_reminder_note(reminder_id, text.strip()[:500])
        return True

    # --- отправка ---

    async def deliver_due(self) -> int:
        """Отправляет всё, чему подошёл срок. Возвращает число отправленных."""
        now = self._now()
        sent = 0
        for row in await self._db.due_reminders(now):
            text = format_reminder(
                row["payload"],
                note=row["note"],
                target=_parse(row["target_at"]),
                stage=row["stage"],
            )
            try:
                await self._send(row["chat_id"], text, row)
            except Exception:
                # Помечаем отправленным всё равно: иначе упавшее напоминание
                # будет повторяться в каждом цикле и превратится в спам.
                log.exception("напоминание %s не доставлено", row["id"])
            await self._db.set_reminder_status(row["id"], "sent")
            sent += 1
        return sent


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment
