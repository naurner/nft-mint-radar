from wlparser.db import Database
from wlparser.notify.commands import (
    GOODBYE,
    WELCOME,
    WELCOME_BACK,
    SubscriptionService,
    format_recent,
)

OWNER = 111
GUEST = 222


async def make(tmp_path, notify=None):
    db = Database(tmp_path / "subs.sqlite")
    await db.connect()
    sent = []

    async def notify_owner(text):
        sent.append(text)
        if notify:
            notify(text)

    service = SubscriptionService(db, owner_chat_id=OWNER, notify_owner=notify_owner)
    return db, service, sent


async def test_first_message_subscribes_and_welcomes(tmp_path):
    db, service, _ = await make(tmp_path)
    text = await service.subscribe(GUEST, username="alice", display_name="Alice")
    assert text == WELCOME
    assert await service.active_chat_ids() == [GUEST]
    await db.close()


async def test_owner_is_notified_about_a_newcomer(tmp_path):
    db, service, sent = await make(tmp_path)
    await service.subscribe(GUEST, username="alice", display_name="Alice")
    assert len(sent) == 1
    assert "Alice" in sent[0]
    assert "@alice" in sent[0]
    assert str(GUEST) in sent[0]
    await db.close()


async def test_newcomer_message_reports_total_count(tmp_path):
    db, service, sent = await make(tmp_path)
    await service.ensure_owner()
    await service.subscribe(GUEST, username=None, display_name="Alice")
    assert "Всего подписчиков: 2" in sent[0]
    await db.close()


async def test_owner_is_not_announced_to_themselves(tmp_path):
    db, service, sent = await make(tmp_path)
    await service.subscribe(OWNER, username="me", display_name="Me")
    assert sent == []
    await db.close()


async def test_returning_user_does_not_trigger_a_second_notification(tmp_path):
    # Один человек — одно уведомление, сколько бы раз он ни возвращался.
    db, service, sent = await make(tmp_path)
    await service.subscribe(GUEST, username="alice", display_name="Alice")
    await service.unsubscribe(GUEST)
    text = await service.subscribe(GUEST, username="alice", display_name="Alice")
    assert text == WELCOME_BACK
    assert len(sent) == 1
    await db.close()


async def test_unsubscribe_removes_from_recipients(tmp_path):
    db, service, _ = await make(tmp_path)
    await service.subscribe(GUEST, username=None, display_name=None)
    assert await service.unsubscribe(GUEST) == GOODBYE
    assert await service.active_chat_ids() == []
    await db.close()


async def test_resubscribe_restores_delivery(tmp_path):
    db, service, _ = await make(tmp_path)
    await service.subscribe(GUEST, username=None, display_name=None)
    await service.unsubscribe(GUEST)
    await service.subscribe(GUEST, username=None, display_name=None)
    assert await service.active_chat_ids() == [GUEST]
    await db.close()


async def test_owner_is_subscribed_without_ever_writing(tmp_path):
    db, service, _ = await make(tmp_path)
    await service.ensure_owner()
    assert await service.active_chat_ids() == [OWNER]
    await db.close()


def test_is_owner_true_only_for_the_owner_chat_id():
    service = SubscriptionService(object(), owner_chat_id=OWNER, notify_owner=None)
    assert service.is_owner(OWNER) is True
    assert service.is_owner(GUEST) is False


def test_is_owner_false_when_no_owner_configured():
    service = SubscriptionService(object(), owner_chat_id=None, notify_owner=None)
    assert service.is_owner(GUEST) is False


async def test_owner_comes_first_in_recipients(tmp_path):
    db, service, _ = await make(tmp_path)
    await service.subscribe(GUEST, username=None, display_name=None)
    await service.ensure_owner()
    assert (await service.active_chat_ids())[0] == OWNER
    await db.close()


async def test_deactivate_stops_delivery(tmp_path):
    db, service, _ = await make(tmp_path)
    await service.subscribe(GUEST, username=None, display_name=None)
    await service.deactivate(GUEST)
    assert await service.active_chat_ids() == []
    await db.close()


async def test_owner_notification_failure_does_not_break_subscription(tmp_path):
    db = Database(tmp_path / "x.sqlite")
    await db.connect()

    async def broken(text):
        raise RuntimeError("owner blocked the bot")

    service = SubscriptionService(db, owner_chat_id=OWNER, notify_owner=broken)
    assert await service.subscribe(GUEST, username=None, display_name=None) == WELCOME
    assert await service.active_chat_ids() == [GUEST]
    await db.close()


async def test_username_is_escaped_in_owner_notification(tmp_path):
    db, service, sent = await make(tmp_path)
    await service.subscribe(GUEST, username=None, display_name="<b>hack</b>")
    assert "<b>hack</b>" not in sent[0]
    assert "&lt;b&gt;" in sent[0]
    await db.close()


# --- /recent ---


def test_format_recent_handles_empty():
    assert "Пока ничего" in format_recent([], 10)


def test_format_recent_lists_findings():
    rows = [{"ts": "2026-09-08T12:00:00", "kind": "mint_started", "text": "Cash Bears"}]
    text = format_recent(rows, 10)
    assert "🚀" in text
    assert "Cash Bears" in text


def test_format_recent_escapes_untrusted_text():
    rows = [{"ts": "2026-09-08T12:00", "kind": "chatter", "text": "<script>x</script>"}]
    assert "<script>" not in format_recent(rows, 10)


def test_format_recent_links_a_tweet_by_its_url():
    rows = [
        {
            "ts": "2026-09-08T12:00",
            "kind": "wl_announced",
            "text": "WL soon",
            "url": "https://x.com/a/status/1",
        }
    ]
    text = format_recent(rows, 10)
    assert 'href="https://x.com/a/status/1"' in text


def test_format_recent_links_onchain_finds_via_opensea():
    rows = [
        {
            "ts": "2026-09-08T12:00",
            "kind": "mint_started",
            "text": "Cash Bears",
            "address": "0x" + "a" * 40,
            "chain": "robinhood",
        }
    ]
    text = format_recent(rows, 10)
    assert "opensea.io/assets/robinhood/" in text


def test_format_recent_has_no_link_when_neither_is_available():
    rows = [{"ts": "2026-09-08T12:00", "kind": "chatter", "text": "just chatter"}]
    assert "href=" not in format_recent(rows, 10)


# --- сборка роутера ---


def test_build_router_accepts_a_tracker_and_still_builds(tmp_path):
    from wlparser.notify.commands import build_router

    db = object()  # роутеру на этом этапе достаточно, что объект существует
    service = object()

    class FakeTracker:
        pass

    router = build_router(service, db, tracker=FakeTracker())
    handlers = sum(len(observer.handlers) for observer in router.observers.values())
    assert handlers > 0


def test_build_router_works_without_a_tracker():
    from wlparser.notify.commands import build_router

    router = build_router(object(), object())
    handlers = sum(len(observer.handlers) for observer in router.observers.values())
    assert handlers > 0


def test_build_router_accepts_a_poster_and_still_builds(tmp_path):
    from wlparser.notify.commands import build_router

    class FakePoster:
        pass

    router = build_router(object(), object(), poster=FakePoster())
    handlers = sum(len(observer.handlers) for observer in router.observers.values())
    assert handlers > 0


def test_build_router_accepts_a_host_updater_and_still_builds():
    from wlparser.notify.commands import build_router

    class FakeHostUpdater:
        pass

    router = build_router(object(), object(), host_updater=FakeHostUpdater())
    handlers = sum(len(observer.handlers) for observer in router.observers.values())
    assert handlers > 0


# --- /menu: статус ---


async def test_build_status_text_reports_counts(tmp_path):
    from datetime import datetime, timezone

    from wlparser.models import Signal
    from wlparser.notify.commands import build_status_text

    db = Database(tmp_path / "status.sqlite")
    await db.connect()
    await db.add_subscriber(111)
    await db.add_subscriber(222)

    address = "0x" + "a" * 40
    sig = Signal(
        source="onchain",
        kind="mint_started",
        text="t",
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        raw={},
    )
    await db.record_signal(sig, score=90)
    await db.start_tracking(address)

    text = await build_status_text(
        db, tracking_enabled=True, poster_enabled=False, twitter_enabled=True
    )

    assert "подписчиков: 2" in text
    assert "отслеживается сейчас: 1" in text
    assert "живое отслеживание: включено" in text
    assert "твиттер-источник: включён" in text
    assert "автопост в X: выключен" in text
    await db.close()


async def test_build_status_text_includes_post_counts_when_poster_enabled(tmp_path):
    from wlparser.notify.commands import build_status_text

    db = Database(tmp_path / "status2.sqlite")
    await db.connect()
    # Реальный адрес контракта — не None: count_drafts_since теперь не
    # считает черновики без адреса (ручные тестовые, см. send_test_draft).
    address = "0x" + "a" * 40
    post_id = await db.create_pending_post(address=address, text="draft")
    await db.decide_post(post_id, status="posted", tweet_url="https://x.com/a/status/1")
    await db.create_pending_post(address=address, text="draft2")  # остаётся pending

    text = await build_status_text(
        db, tracking_enabled=False, poster_enabled=True, twitter_enabled=False
    )

    assert "автопост в X: включён" in text
    assert "предложено за сутки: 2" in text
    assert "ждёт решения: 1" in text
    assert "опубликовано: 1" in text
    assert "процитировано: 0" in text
    await db.close()


async def test_build_status_text_counts_quotes_separately_from_posts(tmp_path):
    from wlparser.notify.commands import build_status_text

    db = Database(tmp_path / "status3.sqlite")
    await db.connect()
    quote_id = await db.create_pending_post(address=None, text="nice find", kind="quote")
    await db.decide_post(quote_id, status="posted", tweet_url="https://x.com/a/status/9")

    text = await build_status_text(
        db, tracking_enabled=False, poster_enabled=True, twitter_enabled=False
    )

    assert "опубликовано: 0" in text
    assert "процитировано: 1" in text
    await db.close()


class FakeHealthLLM:
    def __init__(self, ok, detail):
        self._result = (ok, detail)

    async def health(self):
        return self._result


async def test_status_says_the_local_model_is_off_when_not_configured(tmp_path):
    from wlparser.notify.commands import build_status_text

    db = Database(tmp_path / "llm0.sqlite")
    await db.connect()
    text = await build_status_text(
        db, tracking_enabled=False, poster_enabled=False, twitter_enabled=False
    )
    assert "локальный ИИ: выключен" in text
    await db.close()


async def test_status_shows_a_healthy_local_model(tmp_path):
    from wlparser.notify.commands import build_status_text

    db = Database(tmp_path / "llm1.sqlite")
    await db.connect()
    text = await build_status_text(
        db,
        tracking_enabled=False,
        poster_enabled=False,
        twitter_enabled=False,
        llm=FakeHealthLLM(True, "модель llama3.1"),
    )
    assert "✅ модель llama3.1" in text
    await db.close()


async def test_status_warns_when_the_local_model_is_down(tmp_path):
    from wlparser.notify.commands import build_status_text

    db = Database(tmp_path / "llm2.sqlite")
    await db.connect()
    text = await build_status_text(
        db,
        tracking_enabled=False,
        poster_enabled=False,
        twitter_enabled=False,
        llm=FakeHealthLLM(False, "не отвечает (ConnectError)"),
    )
    assert "⚠️ не отвечает (ConnectError)" in text
    await db.close()


def test_build_router_accepts_an_llm_and_still_builds():
    from wlparser.notify.commands import build_router

    router = build_router(object(), object(), llm=FakeHealthLLM(True, "x"))
    handlers = sum(len(observer.handlers) for observer in router.observers.values())
    assert handlers > 0


def test_build_router_accepts_a_log_path_and_still_builds(tmp_path):
    from wlparser.notify.commands import build_router

    router = build_router(object(), object(), log_path=tmp_path / "bot.log")
    handlers = sum(len(observer.handlers) for observer in router.observers.values())
    assert handlers > 0


def test_build_router_adds_more_handlers_with_a_log_path(tmp_path):
    from wlparser.notify.commands import build_router

    without_logs = build_router(object(), object())
    with_logs = build_router(object(), object(), log_path=tmp_path / "bot.log")

    count = lambda r: sum(len(o.handlers) for o in r.observers.values())
    assert count(with_logs) > count(without_logs)


def test_build_router_adds_more_handlers_with_a_db_path(tmp_path):
    from wlparser.notify.commands import build_router

    without_dump = build_router(object(), object())
    with_dump = build_router(object(), object(), db_path=tmp_path / "wlparser.sqlite")

    count = lambda r: sum(len(o.handlers) for o in r.observers.values())
    assert count(with_dump) > count(without_dump)
