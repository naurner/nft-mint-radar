from datetime import datetime, timedelta, timezone

from wlparser.db import Database
from wlparser.models import Signal

NFT = "0x" + "a" * 40


def signal(kind="contract_deployed", address=NFT, raw=None):
    return Signal(
        source="onchain",
        kind=kind,
        text="t",
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw=raw or {},
    )


async def open_db(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.connect()
    return db


async def test_cursor_roundtrip(tmp_path):
    db = await open_db(tmp_path)
    assert await db.get_cursor("block") is None
    await db.set_cursor("block", "1234")
    assert await db.get_cursor("block") == "1234"
    await db.set_cursor("block", "1250")
    assert await db.get_cursor("block") == "1250"
    await db.close()


async def test_record_signal_creates_project_with_metadata(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(
        signal(raw={"info": {"name": "Onchain Harvest", "symbol": "OCH"}}), score=90
    )
    project = await db.get_project(NFT)
    assert project["address"] == NFT
    assert project["name"] == "Onchain Harvest"
    await db.close()


async def test_later_signal_does_not_erase_known_metadata(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(raw={"info": {"name": "Onchain Harvest"}}), score=90)
    await db.record_signal(signal(kind="mint_started", raw={}), score=90)
    project = await db.get_project(NFT)
    assert project["name"] == "Onchain Harvest"
    await db.close()


async def test_mark_seen_is_idempotent(tmp_path):
    db = await open_db(tmp_path)
    assert await db.mark_seen("key-1") is True
    assert await db.mark_seen("key-1") is False
    await db.close()


async def test_threads_are_kept_per_chat(tmp_path):
    # Один контракт, разные чаты — разные message_id.
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.set_thread_id(111, NFT, 555)
    await db.set_thread_id(222, NFT, 777)
    assert await db.load_threads() == {(111, NFT): 555, (222, NFT): 777}
    await db.close()


async def test_thread_id_is_overwritten_not_duplicated(tmp_path):
    db = await open_db(tmp_path)
    await db.set_thread_id(111, NFT, 555)
    await db.set_thread_id(111, NFT, 999)
    assert await db.load_threads() == {(111, NFT): 999}
    await db.close()


async def test_new_subscriber_is_reported_as_new_only_once(tmp_path):
    db = await open_db(tmp_path)
    assert await db.add_subscriber(111, username="a") is True
    assert await db.add_subscriber(111, username="a") is False
    await db.close()


async def test_inactive_subscriber_is_excluded_from_the_list(tmp_path):
    db = await open_db(tmp_path)
    await db.add_subscriber(111)
    await db.add_subscriber(222)
    await db.set_subscriber_active(222, False)
    assert [row["chat_id"] for row in await db.list_subscribers()] == [111]
    assert await db.count_subscribers() == 1
    await db.close()


async def test_owner_flag_is_never_lost_on_resubscribe(tmp_path):
    db = await open_db(tmp_path)
    await db.add_subscriber(111, is_owner=True)
    await db.add_subscriber(111, username="me")
    rows = await db.list_subscribers()
    assert rows[0]["is_owner"] == 1
    await db.close()


async def test_owner_sorts_first(tmp_path):
    db = await open_db(tmp_path)
    await db.add_subscriber(222)
    await db.add_subscriber(111, is_owner=True)
    assert [row["chat_id"] for row in await db.list_subscribers()][0] == 111
    await db.close()


async def test_recent_returns_newest_first(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(kind="contract_deployed"), score=50)
    await db.record_signal(signal(kind="mint_started"), score=80)
    rows = await db.recent_signals(limit=10)
    assert rows[0]["kind"] == "mint_started"
    await db.close()


# --- слежение и снимки истории ---


async def test_start_tracking_marks_project_active(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    assert await db.start_tracking(NFT) is True
    project = await db.get_project(NFT)
    assert project["tracking_status"] == "active"
    assert project["tracking_started_at"] is not None
    await db.close()


async def test_start_tracking_twice_is_a_noop(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    assert await db.start_tracking(NFT) is True
    assert await db.start_tracking(NFT) is False
    await db.close()


async def test_start_tracking_unknown_project_does_nothing(tmp_path):
    db = await open_db(tmp_path)
    assert await db.start_tracking(NFT) is False
    await db.close()


async def test_active_tracked_projects_excludes_untracked(tmp_path):
    db = await open_db(tmp_path)
    other = "0x" + "b" * 40
    await db.record_signal(signal(address=NFT), score=90)
    await db.record_signal(signal(address=other), score=90)
    await db.start_tracking(NFT)
    addresses = {p["address"] for p in await db.active_tracked_projects()}
    assert addresses == {NFT}
    await db.close()


async def test_set_tracking_status_stops_it_from_active_list(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.start_tracking(NFT)
    await db.set_tracking_status(NFT, "sold_out")
    assert await db.active_tracked_projects() == []
    await db.close()


async def test_bump_stale_checks_increments(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.start_tracking(NFT)
    assert await db.bump_stale_checks(NFT) == 1
    assert await db.bump_stale_checks(NFT) == 2
    await db.close()


async def test_reset_stale_checks_zeroes_the_counter(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.start_tracking(NFT)
    await db.bump_stale_checks(NFT)
    await db.reset_stale_checks(NFT)
    assert (await db.get_project(NFT))["stale_checks"] == 0
    await db.close()


async def test_opensea_slug_is_set_once_and_not_overwritten(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.set_opensea_slug(NFT, "first-slug")
    await db.set_opensea_slug(NFT, "second-slug")
    assert (await db.get_project(NFT))["opensea_slug"] == "first-slug"
    await db.close()


async def test_interest_score_and_top_projects(tmp_path):
    db = await open_db(tmp_path)
    other = "0x" + "b" * 40
    now = datetime.now(timezone.utc)
    await db.record_signal(_signal_at(NFT, now), score=90)
    await db.record_signal(_signal_at(other, now), score=90)
    await db.set_interest_score(NFT, 80, "флор выше минта")
    await db.set_interest_score(other, 40, "мало сигналов")
    top = await db.top_projects(10)
    assert [p["address"] for p in top] == [NFT, other]
    await db.close()


async def test_near_sellout_alerted_defaults_to_falsy_and_can_be_marked(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    assert not (await db.get_project(NFT))["near_sellout_alerted"]

    await db.mark_near_sellout_alerted(NFT)
    assert (await db.get_project(NFT))["near_sellout_alerted"]
    await db.close()


async def test_projects_without_a_score_are_excluded_from_top(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    assert await db.top_projects(10) == []
    await db.close()


async def test_threads_for_address_returns_every_chat(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.set_thread_id(111, NFT, 500)
    await db.set_thread_id(222, NFT, 700)
    pairs = set(await db.threads_for_address(NFT))
    assert pairs == {(111, 500), (222, 700)}
    await db.close()


async def test_delete_thread_removes_only_that_pair(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.set_thread_id(111, NFT, 500)
    await db.set_thread_id(222, NFT, 700)
    await db.delete_thread(111, NFT)
    pairs = await db.threads_for_address(NFT)
    assert pairs == [(222, 700)]
    await db.close()


async def test_add_snapshot_and_latest_snapshot(tmp_path):
    from datetime import datetime, timezone

    db = await open_db(tmp_path)
    await db.add_snapshot(
        address=NFT,
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_supply=10,
        max_supply=100,
        mint_price_wei=0,
        floor_price_eth=0.02,
        unique_minters=8,
        num_owners=5,
        source="tracker",
    )
    latest = await db.latest_snapshot(NFT)
    assert latest["total_supply"] == 10
    assert latest["floor_price_eth"] == 0.02
    await db.close()


async def test_snapshot_history_is_chronological(tmp_path):
    from datetime import datetime, timezone

    db = await open_db(tmp_path)
    await db.add_snapshot(address=NFT, ts=datetime(2026, 1, 1, tzinfo=timezone.utc), total_supply=1)
    await db.add_snapshot(address=NFT, ts=datetime(2026, 1, 2, tzinfo=timezone.utc), total_supply=5)
    history = await db.snapshot_history(NFT)
    assert [row["total_supply"] for row in history] == [1, 5]
    await db.close()


async def test_latest_snapshot_is_none_without_history(tmp_path):
    db = await open_db(tmp_path)
    assert await db.latest_snapshot(NFT) is None
    await db.close()


async def test_save_links_persists_opensea_slug(tmp_path):
    from wlparser.enrich.models import ProjectLinks

    db = await open_db(tmp_path)
    await db.record_signal(signal(), score=90)
    await db.save_links(NFT, ProjectLinks(collection_slug="cash-bears"))
    assert (await db.get_project(NFT))["opensea_slug"] == "cash-bears"
    await db.close()


# --- защита от переполнения SQLite INTEGER ---
# Живой баг: контракт вернул огромное число на угаданной сигнатуре цены/лимита,
# и запись в базу падала с OverflowError. Значения за пределами 64-битного
# INTEGER должны молча превращаться в None, а не ронять конвейер.


HUGE = 2**70  # заведомо больше, чем может представить SQLite INTEGER


async def test_record_signal_survives_huge_mint_price(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(
        signal(raw={"info": {"name": "Broken", "mint_price_wei": HUGE}}), score=90
    )
    project = await db.get_project(NFT)
    assert project["mint_price_wei"] is None
    await db.close()


async def test_record_signal_survives_huge_total_supply(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(
        signal(raw={"info": {"name": "Broken", "total_supply": HUGE}}), score=90
    )
    project = await db.get_project(NFT)
    assert project["total_supply"] is None
    await db.close()


async def test_record_signal_keeps_a_normal_price_intact(tmp_path):
    db = await open_db(tmp_path)
    normal_price = 10**16  # 0.01 ETH — обычная цена, не должна пострадать
    await db.record_signal(
        signal(raw={"info": {"name": "Fine", "mint_price_wei": normal_price}}), score=90
    )
    project = await db.get_project(NFT)
    assert project["mint_price_wei"] == normal_price
    await db.close()


async def test_add_snapshot_survives_huge_values(tmp_path):
    from datetime import datetime, timezone

    db = await open_db(tmp_path)
    await db.add_snapshot(
        address=NFT,
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_supply=HUGE,
        max_supply=HUGE,
        mint_price_wei=HUGE,
        floor_price_eth=0.1,
    )
    row = await db.latest_snapshot(NFT)
    assert row["total_supply"] is None
    assert row["max_supply"] is None
    assert row["mint_price_wei"] is None
    assert row["floor_price_eth"] == 0.1  # float-поле не тронуто
    await db.close()


# --- поиск по названию/тикеру ---


async def test_search_projects_matches_by_name(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(raw={"info": {"name": "Cash Bears"}}), score=90)
    results = await db.search_projects("cash")
    assert [r["address"] for r in results] == [NFT]
    await db.close()


async def test_search_projects_is_case_insensitive(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(raw={"info": {"name": "Cash Bears"}}), score=90)
    assert len(await db.search_projects("CASH")) == 1
    await db.close()


async def test_search_projects_matches_by_symbol_too(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(raw={"info": {"name": "Something", "symbol": "CBRS"}}), score=90)
    assert len(await db.search_projects("CBRS")) == 1
    await db.close()


async def test_search_projects_no_match_is_empty(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(signal(raw={"info": {"name": "Cash Bears"}}), score=90)
    assert await db.search_projects("ghost") == []
    await db.close()


async def test_search_projects_respects_limit(tmp_path):
    db = await open_db(tmp_path)
    for i in range(5):
        addr = "0x" + str(i) * 40
        await db.record_signal(signal(address=addr, raw={"info": {"name": f"Cash {i}"}}), score=90)
    assert len(await db.search_projects("Cash", limit=2)) == 2
    await db.close()


# --- top_projects ---


def _signal_at(address: str, when: datetime) -> Signal:
    return Signal(
        source="onchain",
        kind="contract_deployed",
        text="t",
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=when,
        raw={},
    )


async def test_top_projects_excludes_stale_entries(tmp_path):
    # Без окна по времени рейтинг только рос бы — раздачи без активности
    # месяцами продолжали бы заслонять собой то, что происходит сейчас.
    db = await open_db(tmp_path)
    now = datetime.now(timezone.utc)
    fresh, stale = "0x" + "1" * 40, "0x" + "2" * 40

    await db.record_signal(_signal_at(fresh, now), score=90)
    await db.set_interest_score(fresh, 80, "активен")

    await db.record_signal(_signal_at(stale, now - timedelta(days=30)), score=90)
    await db.set_interest_score(stale, 95, "давно тихо")

    rows = await db.top_projects(within_days=14)

    assert [r["address"] for r in rows] == [fresh]
    await db.close()


async def test_top_projects_default_window_keeps_recent_entries(tmp_path):
    db = await open_db(tmp_path)
    await db.record_signal(_signal_at(NFT, datetime.now(timezone.utc)), score=90)
    await db.set_interest_score(NFT, 70, "норм")

    rows = await db.top_projects()

    assert [r["address"] for r in rows] == [NFT]
    await db.close()


# --- посты в X ---


async def test_pending_post_can_be_created_and_fetched(tmp_path):
    db = await open_db(tmp_path)
    post_id = await db.create_pending_post(address=NFT, text="draft text")
    post = await db.get_post(post_id)
    assert post["address"] == NFT
    assert post["text"] == "draft text"
    assert post["status"] == "pending"
    assert post["tweet_url"] is None
    assert post["kind"] == "post"
    await db.close()


async def test_pending_post_can_be_created_as_a_quote(tmp_path):
    db = await open_db(tmp_path)
    post_id = await db.create_pending_post(address=None, text="nice find", kind="quote")
    post = await db.get_post(post_id)
    assert post["kind"] == "quote"
    await db.close()


async def test_count_posts_since_separates_posts_and_quotes(tmp_path):
    db = await open_db(tmp_path)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)

    post_id = await db.create_pending_post(address=NFT, text="a")
    await db.decide_post(post_id, status="posted", tweet_url="https://x.com/a/status/1")
    quote_id = await db.create_pending_post(address=None, text="b", kind="quote")
    await db.decide_post(quote_id, status="posted", tweet_url="https://x.com/a/status/2")

    assert await db.count_posts_since(since) == 1
    assert await db.count_posts_since(since, kind="quote") == 1
    await db.close()


async def test_count_drafts_since_separates_posts_and_quotes(tmp_path):
    db = await open_db(tmp_path)
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)

    await db.create_pending_post(address=NFT, text="a")
    await db.create_pending_post(address=NFT, text="b", kind="quote")

    assert await db.count_drafts_since(since) == 1
    assert await db.count_drafts_since(since, kind="quote") == 1
    await db.close()


async def test_decide_post_marks_posted_with_url(tmp_path):
    db = await open_db(tmp_path)
    post_id = await db.create_pending_post(address=NFT, text="draft text")
    await db.decide_post(post_id, status="posted", tweet_url="https://x.com/a/status/1")
    post = await db.get_post(post_id)
    assert post["status"] == "posted"
    assert post["tweet_url"] == "https://x.com/a/status/1"
    assert post["decided_at"] is not None
    await db.close()


async def test_decide_post_can_mark_skipped(tmp_path):
    db = await open_db(tmp_path)
    post_id = await db.create_pending_post(address=NFT, text="draft text")
    await db.decide_post(post_id, status="skipped")
    post = await db.get_post(post_id)
    assert post["status"] == "skipped"
    assert post["tweet_url"] is None
    await db.close()


async def test_count_posts_since_counts_only_posted(tmp_path):
    db = await open_db(tmp_path)
    p1 = await db.create_pending_post(address=NFT, text="a")
    p2 = await db.create_pending_post(address=NFT, text="b")
    p3 = await db.create_pending_post(address=NFT, text="c")
    await db.decide_post(p1, status="posted", tweet_url="https://x.com/a/status/1")
    await db.decide_post(p2, status="skipped")
    # p3 остаётся pending

    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert await db.count_posts_since(since) == 1
    await db.close()


async def test_count_posts_since_respects_the_time_window(tmp_path):
    db = await open_db(tmp_path)
    post_id = await db.create_pending_post(address=NFT, text="a")
    await db.decide_post(post_id, status="posted", tweet_url="https://x.com/a/status/1")

    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert await db.count_posts_since(future) == 0
    await db.close()


async def test_count_pending_posts_counts_only_pending(tmp_path):
    db = await open_db(tmp_path)
    p1 = await db.create_pending_post(address=NFT, text="a")
    await db.create_pending_post(address=NFT, text="b")  # остаётся pending
    await db.create_pending_post(address=NFT, text="c")  # остаётся pending
    await db.decide_post(p1, status="posted", tweet_url="https://x.com/a/status/1")

    assert await db.count_pending_posts() == 2
    await db.close()


async def test_count_drafts_since_counts_every_status(tmp_path):
    db = await open_db(tmp_path)
    p1 = await db.create_pending_post(address=NFT, text="a")
    p2 = await db.create_pending_post(address=NFT, text="b")
    await db.create_pending_post(address=NFT, text="c")  # остаётся pending
    await db.decide_post(p1, status="posted", tweet_url="https://x.com/a/status/1")
    await db.decide_post(p2, status="skipped")

    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert await db.count_drafts_since(since) == 3
    await db.close()


async def test_count_drafts_since_respects_the_time_window(tmp_path):
    db = await open_db(tmp_path)
    await db.create_pending_post(address=NFT, text="a")

    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert await db.count_drafts_since(future) == 0
    await db.close()


async def test_count_drafts_since_excludes_manual_test_drafts(tmp_path):
    # send_test_draft создаёт черновики с address=None и намеренно не
    # должно отъедать дневной лимит у настоящих находок — реальный сигнал
    # всегда несёт адрес контракта.
    db = await open_db(tmp_path)
    await db.create_pending_post(address=NFT, text="real")
    await db.create_pending_post(address=None, text="test 1")
    await db.create_pending_post(address=None, text="test 2")

    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert await db.count_drafts_since(since) == 1
    await db.close()


async def test_clear_pending_posts_skips_only_pending_ones(tmp_path):
    db = await open_db(tmp_path)
    p1 = await db.create_pending_post(address=NFT, text="a")
    p2 = await db.create_pending_post(address=NFT, text="b")
    await db.create_pending_post(address=NFT, text="c")  # остаётся pending
    await db.decide_post(p1, status="posted", tweet_url="https://x.com/a/status/1")
    await db.decide_post(p2, status="skipped")

    cleared = await db.clear_pending_posts()

    assert cleared == 1
    assert await db.count_pending_posts() == 0
    posted = await db.get_post(p1)
    assert posted["status"] == "posted"  # уже опубликованный не тронут
    await db.close()


async def test_clear_pending_posts_with_nothing_pending(tmp_path):
    db = await open_db(tmp_path)
    assert await db.clear_pending_posts() == 0
    await db.close()


# --- copy_all_rows_to (перенос Turso -> локальный файл) ---


async def test_copy_all_rows_to_moves_every_table(tmp_path):
    source = Database(tmp_path / "source.sqlite")
    await source.connect()
    destination = Database(tmp_path / "dest.sqlite")
    await destination.connect()

    await source.record_signal(signal(), score=90)
    await source.add_subscriber(111, username="alice", display_name="Alice")
    await source.set_cursor("block", "12345")

    counts = await source.copy_all_rows_to(destination)

    assert counts["signals"] == 1
    assert counts["projects"] == 1
    assert counts["subscribers"] == 1
    assert counts["cursors"] == 1

    assert await destination.get_cursor("block") == "12345"
    assert (await destination.get_project(NFT))["address"] == NFT
    rows = await destination.list_subscribers()
    assert rows[0]["username"] == "alice"

    await source.close()
    await destination.close()


async def test_copy_all_rows_to_does_not_duplicate_existing_rows(tmp_path):
    source = Database(tmp_path / "source.sqlite")
    await source.connect()
    destination = Database(tmp_path / "dest.sqlite")
    await destination.connect()

    await source.set_cursor("block", "999")
    await destination.set_cursor("block", "999")  # уже есть в destination

    counts = await source.copy_all_rows_to(destination)

    assert counts["cursors"] == 1  # прочитано из source
    assert await destination.get_cursor("block") == "999"  # не задвоилось/не упало

    await source.close()
    await destination.close()


# --- digest_projects_since / digest_announcements_since ---


async def test_digest_projects_since_excludes_stale_entries(tmp_path):
    db = await open_db(tmp_path)
    now = datetime.now(timezone.utc)
    fresh, stale = "0x" + "1" * 40, "0x" + "2" * 40

    await db.record_signal(_signal_at(fresh, now), score=90)
    await db.record_signal(_signal_at(stale, now - timedelta(days=2)), score=90)

    rows = await db.digest_projects_since(now - timedelta(hours=1))

    assert [r["address"] for r in rows] == [fresh]
    await db.close()


async def test_digest_projects_since_orders_by_score_then_recency(tmp_path):
    db = await open_db(tmp_path)
    now = datetime.now(timezone.utc)
    low, high, unscored = "0x" + "3" * 40, "0x" + "4" * 40, "0x" + "5" * 40

    await db.record_signal(_signal_at(low, now), score=90)
    await db.set_interest_score(low, 20, "так себе")
    await db.record_signal(_signal_at(high, now), score=90)
    await db.set_interest_score(high, 90, "огонь")
    await db.record_signal(_signal_at(unscored, now), score=90)  # ещё не оценён

    rows = await db.digest_projects_since(now - timedelta(hours=1))

    assert [r["address"] for r in rows] == [high, low, unscored]
    await db.close()


async def test_digest_announcements_since_only_wl_announced_in_window(tmp_path):
    db = await open_db(tmp_path)
    now = datetime.now(timezone.utc)

    async def make(kind, when, text):
        sig = Signal(
            source="twitter",
            kind=kind,
            text=text,
            url=None,
            author="someone",
            contract_address=None,
            chain=None,
            timestamp=when,
            raw={},
        )
        await db.record_signal(sig, score=50)

    await make("wl_announced", now, "fresh WL")
    await make("wl_announced", now - timedelta(days=2), "stale WL")
    await make("chatter", now, "just chatter")

    rows = await db.digest_announcements_since(now - timedelta(hours=1))

    assert [r["text"] for r in rows] == ["fresh WL"]
    await db.close()
