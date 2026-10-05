from datetime import datetime, timedelta, timezone

from wlparser.db import Database
from wlparser.notify.reminders import (
    FINAL_LEAD_MINUTES,
    MIN_DELAY,
    PRESET_BY_CODE,
    ReminderService,
    compute_due,
    format_reminder,
    humanize,
)

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
CHAT = 111
ALERT = "🚀 <b>Начался минт</b>\nMint confirmed: Cash Bears, 34 wallets"


class FakeSender:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def __call__(self, chat_id, text, row):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, row))


async def service(tmp_path, *, now=T0, sender=None, lead=10):
    db = Database(tmp_path / "rem.sqlite")
    await db.connect()
    clock = {"now": now}
    svc = ReminderService(
        db, sender or FakeSender(), lead_minutes=lead, now=lambda: clock["now"]
    )
    return db, svc, clock


# --- расчёт времени ---


def test_reminder_lands_before_the_event():
    target = T0 + timedelta(hours=1)
    assert compute_due(target, lead_minutes=10, now=T0) == target - timedelta(minutes=10)


def test_short_interval_still_produces_a_reminder():
    """До события меньше времени опережения — напоминание не теряется."""
    target = T0 + timedelta(minutes=5)
    due = compute_due(target, lead_minutes=10, now=T0)
    assert due == T0 + MIN_DELAY
    assert due < target


def test_reminder_never_fires_instantly():
    due = compute_due(T0, lead_minutes=10, now=T0)
    assert due >= T0 + MIN_DELAY


def test_humanize_reads_naturally():
    assert humanize(timedelta(minutes=45)) == "45 мин"
    assert humanize(timedelta(hours=2)) == "2 ч"
    assert humanize(timedelta(hours=2, minutes=30)) == "2 ч 30 мин"
    assert humanize(timedelta(seconds=10)) == "меньше минуты"


def test_presets_cover_the_useful_range():
    assert PRESET_BY_CODE["15m"].minutes == 15
    assert PRESET_BY_CODE["24h"].minutes == 1440


# --- текст напоминания ---


def test_reminder_repeats_the_original_alert_in_full():
    """Ради этого всё и делается: не искать оригинал в переписке."""
    text = format_reminder(ALERT, note=None, target=T0, stage="lead")
    assert ALERT in text
    assert "Напоминание" in text


def test_note_is_shown_above_the_alert():
    text = format_reminder(ALERT, note="проверить цену", target=T0, stage="lead")
    assert "проверить цену" in text
    assert text.index("проверить цену") < text.index("Mint confirmed")


def test_note_is_escaped():
    text = format_reminder(ALERT, note="<script>x</script>", target=T0, stage="lead")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_final_reminder_says_it_is_the_last_one():
    text = format_reminder(ALERT, note=None, target=T0, stage="final")
    assert "Через минуту" in text


# --- постановка и доставка ---


async def test_schedule_stores_a_pending_reminder(tmp_path):
    db, svc, _ = await service(tmp_path)
    rid, due, target = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    row = await db.get_reminder(rid)
    assert row["status"] == "pending"
    assert row["chat_id"] == CHAT
    assert row["payload"] == ALERT
    assert target - due == timedelta(minutes=10)
    await db.close()


async def test_nothing_is_delivered_before_its_time(tmp_path):
    sender = FakeSender()
    db, svc, clock = await service(tmp_path, sender=sender)
    await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    assert await svc.deliver_due() == 0
    assert sender.sent == []
    await db.close()


async def test_reminder_is_delivered_once_its_time_comes(tmp_path):
    sender = FakeSender()
    db, svc, clock = await service(tmp_path, sender=sender)
    await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    clock["now"] = T0 + timedelta(minutes=51)
    assert await svc.deliver_due() == 1
    chat_id, text, _row = sender.sent[0]
    assert chat_id == CHAT
    assert "Cash Bears" in text
    await db.close()


async def test_a_delivered_reminder_never_repeats(tmp_path):
    sender = FakeSender()
    db, svc, clock = await service(tmp_path, sender=sender)
    await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    clock["now"] = T0 + timedelta(minutes=51)
    await svc.deliver_due()
    await svc.deliver_due()
    assert len(sender.sent) == 1
    await db.close()


async def test_failed_delivery_is_not_retried_forever(tmp_path):
    # Иначе упавшее напоминание повторялось бы в каждом цикле.
    db, svc, clock = await service(tmp_path, sender=FakeSender(fail=True))
    rid, _, _ = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    clock["now"] = T0 + timedelta(minutes=51)
    await svc.deliver_due()
    assert (await db.get_reminder(rid))["status"] == "sent"
    assert await svc.deliver_due() == 0
    await db.close()


# --- повтор за минуту ---


async def test_snooze_schedules_a_second_reminder_before_the_event(tmp_path):
    sender = FakeSender()
    db, svc, clock = await service(tmp_path, sender=sender)
    rid, _, target = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)

    clock["now"] = T0 + timedelta(minutes=51)
    await svc.deliver_due()

    due = await svc.schedule_final(rid)
    assert due == target - timedelta(minutes=FINAL_LEAD_MINUTES)

    clock["now"] = target
    assert await svc.deliver_due() == 1
    assert "Через минуту" in sender.sent[-1][1]
    await db.close()


async def test_snooze_keeps_the_note_and_the_original_alert(tmp_path):
    sender = FakeSender()
    db, svc, clock = await service(tmp_path, sender=sender)
    rid, _, target = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    svc.expect_note(CHAT, rid)
    await svc.attach_note(CHAT, "проверить цену перед стартом")

    await svc.schedule_final(rid)
    clock["now"] = target
    await svc.deliver_due()
    text = sender.sent[-1][1]
    assert "проверить цену перед стартом" in text
    assert "Cash Bears" in text
    await db.close()


async def test_snooze_on_a_missing_reminder_is_harmless(tmp_path):
    db, svc, _ = await service(tmp_path)
    assert await svc.schedule_final(9999) is None
    await db.close()


# --- заметки ---


async def test_note_is_attached_to_the_right_reminder(tmp_path):
    db, svc, _ = await service(tmp_path)
    rid, _, _ = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    assert not svc.is_awaiting_note(CHAT)
    svc.expect_note(CHAT, rid)
    assert svc.is_awaiting_note(CHAT)
    assert await svc.attach_note(CHAT, "  посмотреть флор  ")
    assert (await db.get_reminder(rid))["note"] == "посмотреть флор"
    assert not svc.is_awaiting_note(CHAT)
    await db.close()


async def test_text_without_a_pending_note_is_ignored(tmp_path):
    db, svc, _ = await service(tmp_path)
    assert not await svc.attach_note(CHAT, "просто сообщение")
    await db.close()


async def test_long_note_is_trimmed(tmp_path):
    db, svc, _ = await service(tmp_path)
    rid, _, _ = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=60)
    svc.expect_note(CHAT, rid)
    await svc.attach_note(CHAT, "ф" * 900)
    assert len((await db.get_reminder(rid))["note"]) == 500
    await db.close()


async def test_notes_of_different_chats_do_not_mix(tmp_path):
    db, svc, _ = await service(tmp_path)
    first, _, _ = await svc.schedule(chat_id=111, payload=ALERT, minutes=60)
    second, _, _ = await svc.schedule(chat_id=222, payload=ALERT, minutes=60)
    svc.expect_note(111, first)
    svc.expect_note(222, second)
    await svc.attach_note(222, "заметка второго")
    assert (await db.get_reminder(second))["note"] == "заметка второго"
    assert (await db.get_reminder(first))["note"] is None
    await db.close()


# --- хранилище ---


async def test_pending_list_is_per_chat(tmp_path):
    db, svc, _ = await service(tmp_path)
    await svc.schedule(chat_id=111, payload=ALERT, minutes=60)
    await svc.schedule(chat_id=222, payload=ALERT, minutes=60)
    assert len(await db.pending_reminders(111)) == 1
    await db.close()


async def test_reminders_survive_a_restart(tmp_path):
    # Напоминание на завтра должно пережить перезапуск бота.
    db, svc, _ = await service(tmp_path)
    rid, _, _ = await svc.schedule(chat_id=CHAT, payload=ALERT, minutes=1440)
    await db.close()

    reopened = Database(tmp_path / "rem.sqlite")
    await reopened.connect()
    assert (await reopened.get_reminder(rid))["status"] == "pending"
    await reopened.close()
