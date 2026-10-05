"""LiveTracker: живая карточка вместо новых сообщений.

Фейковая сеть и фейковый редактор — проверяем логику решений
(когда править, когда остановиться), а не сетевые вызовы.
"""

from datetime import datetime, timedelta, timezone

from wlparser.chain.selectors import SEL_NAME, SEL_SUPPORTS_INTERFACE, SEL_SYMBOL
from wlparser.db import Database
from wlparser.tracking.service import LiveTracker

NFT = "0x" + "a" * 40
WORD_TRUE = "0x" + "0" * 63 + "1"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def string_word(text: str) -> str:
    word = lambda n: format(n, "064x")  # noqa: E731
    return "0x" + word(32) + word(len(text)) + text.encode().hex().ljust(64, "0")


class FakeChain:
    """Отвечает на inspect()/with_pricing() фиксированными значениями,
    которые тест может менять между тиками."""

    def __init__(self, *, total_supply=10, max_supply=100, price_wei=0):
        self.total_supply = total_supply
        self.max_supply = max_supply
        self.price_wei = price_wei

    async def batch(self, calls):
        out = []
        for _method, params in calls:
            data = params[0]["data"]
            if data.startswith(SEL_SUPPORTS_INTERFACE):
                out.append(WORD_TRUE if "80ac58cd" in data else None)
            elif data.startswith(SEL_NAME):
                out.append(string_word("Cash Bears"))
            elif data.startswith(SEL_SYMBOL):
                out.append(string_word("CB"))
            elif "18160ddd" in data:  # totalSupply()
                out.append("0x" + format(self.total_supply, "064x"))
            else:
                # Один из перебираемых селекторов цены/лимита отвечает первым.
                out.append(None)
        return out


class ScriptedInspector:
    """Полностью управляет тем, что вернёт inspect/with_pricing на каждом тике.

    Реальный ContractInspector перебирает сигнатуры эвристикой — для теста
    решений трекера это не важно, важно только какие значения он видит.
    """

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    async def inspect(self, address):
        from wlparser.chain.contracts import ContractInfo

        state = self.states[min(self.calls, len(self.states) - 1)]
        return ContractInfo(
            address=address,
            standard="erc721",
            name=state.get("name", "Cash Bears"),
            symbol=state.get("symbol", "CB"),
        )

    async def with_pricing(self, info):
        from dataclasses import replace

        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return replace(
            info,
            total_supply=state["total_supply"],
            max_supply=state.get("max_supply", 100),
            mint_price_wei=state.get("mint_price_wei", 0),
        )


class FakeEditor:
    def __init__(self):
        self.edits = []
        self.gone = []

    async def edit(self, chat_id, message_id, address, text):
        self.edits.append((chat_id, message_id, address, text))


class FakeOpenSea:
    def __init__(self, *, slug="cash-bears", stats=None, sales=None, links=None):
        self.slug = slug
        self.stats = stats or {}
        self.sales = sales or {}
        self.links = links or {}
        self.stats_calls = 0
        self.sales_calls = 0
        self.links_calls = 0

    async def collection_slug(self, address):
        return self.slug

    async def collection_stats(self, slug):
        self.stats_calls += 1
        return self.stats.get(slug)

    async def collection_recent_sales(self, slug, *, limit=50):
        self.sales_calls += 1
        return self.sales.get(slug)

    async def collection_links(self, slug):
        from wlparser.enrich.models import ProjectLinks

        self.links_calls += 1
        return self.links.get(slug, ProjectLinks())


def make_tracker(db, inspector, editor, *, opensea=None, **kwargs):
    # По умолчанию 0 — иначе повторный tick() в том же тесте (без реально
    # прошедшего времени) пропустил бы проект как "ещё не пора" (см.
    # LiveTracker._is_due). Тесты про саму адаптивную частоту явно
    # передают ненулевой refresh_interval_s.
    kwargs.setdefault("refresh_interval_s", 0.0)
    tracker = LiveTracker(db, client=None, editor=editor, opensea=opensea, **kwargs)
    tracker._inspector = inspector  # подменяем реальный на управляемый в тесте
    return tracker


async def seed_project(db, address=NFT, **extra):
    from wlparser.models import Signal

    signal = Signal(
        source="onchain",
        kind="mint_started",
        text="Mint confirmed: Cash Bears",
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=T0,
        raw={"info": {"name": "Cash Bears", "symbol": "CB", "standard": "erc721",
                       "max_supply": 100, "mint_price_wei": 0, **extra}},
    )
    await db.record_signal(signal, score=90)
    await db.set_thread_id(111, address, 500)


async def test_track_starts_tracking_once(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)

    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 10}]), FakeEditor())
    await tracker.track(NFT)
    project = await db.get_project(NFT)
    assert project["tracking_status"] == "active"
    assert project["tracking_started_at"] is not None
    await db.close()


async def test_first_tick_always_edits_even_without_prior_snapshot(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 10}]), editor)
    await tracker.tick()

    assert len(editor.edits) == 1
    chat_id, message_id, address, text = editor.edits[0]
    assert (chat_id, message_id) == (111, 500)
    assert address == NFT
    assert "10 / 100" in text
    await db.close()


async def test_unchanged_state_does_not_trigger_a_new_edit(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10}, {"total_supply": 10}])
    tracker = make_tracker(db, inspector, editor)
    await tracker.tick()
    await tracker.tick()

    assert len(editor.edits) == 1  # второй тик ничего не изменил
    await db.close()


async def test_changed_supply_triggers_a_fresh_edit(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10}, {"total_supply": 40}])
    tracker = make_tracker(db, inspector, editor)
    await tracker.tick()
    await tracker.tick()

    assert len(editor.edits) == 2
    assert "40 / 100" in editor.edits[1][3]
    await db.close()


async def test_snapshot_is_recorded_every_tick_regardless_of_edit(tmp_path):
    # История нужна для будущей модели — пишется всегда, даже когда молчим.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    inspector = ScriptedInspector([{"total_supply": 10}, {"total_supply": 10}])
    tracker = make_tracker(db, inspector, FakeEditor())
    await tracker.tick()
    await tracker.tick()

    history = await db.snapshot_history(NFT)
    assert len(history) == 2
    await db.close()


async def test_sold_out_marks_status_and_sends_final_edit(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 100, "max_supply": 100}])
    tracker = make_tracker(db, inspector, editor)
    await tracker.tick()

    assert "Распродано" in editor.edits[-1][3]
    assert (await db.get_project(NFT))["tracking_status"] == "sold_out"
    await db.close()


async def test_junk_contract_with_absurd_supply_stops_tracking(tmp_path):
    # Живой пример: карточка с именем-адресом и саплаем ~1e28 — контракт,
    # где supportsInterface(ERC721) соврал "да", а на деле это не NFT
    # (похоже на ERC-20, у которого totalSupply() легко даёт такие числа).
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10**28, "max_supply": 100}])
    tracker = make_tracker(db, inspector, editor)
    await tracker.tick()

    assert "не настоящая NFT-коллекция" in editor.edits[-1][3]
    assert (await db.get_project(NFT))["tracking_status"] == "invalid"
    await db.close()


async def test_junk_contract_without_a_name_or_symbol_stops_tracking(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector(
        [{"total_supply": 10, "name": None, "symbol": None}]
    )
    tracker = make_tracker(db, inspector, editor)
    await tracker.tick()

    assert "не настоящая NFT-коллекция" in editor.edits[-1][3]
    assert (await db.get_project(NFT))["tracking_status"] == "invalid"
    await db.close()


async def test_invalid_projects_are_no_longer_actively_ticked(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10**28, "max_supply": 100}])
    tracker = make_tracker(db, inspector, editor)
    await tracker.tick()
    await tracker.tick()

    assert len(editor.edits) == 1  # второй тик уже не трогает invalid-проект
    await db.close()


class SignalRecorder:
    def __init__(self):
        self.signals = []

    async def __call__(self, signal):
        self.signals.append(signal)


async def test_near_sellout_alert_does_not_fire_once_fully_sold_out(tmp_path):
    # Живая жалоба: "почти распродано" приходило уже ПОСЛЕ того, как всё
    # распродано — например, если самая первая проверка нового проекта
    # застаёт его уже на 100% (быстрый минт). "Распродано" сообщает об этом
    # отдельная правка карточки — второй, противоречащий алерт не нужен.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    recorder = SignalRecorder()
    inspector = ScriptedInspector([{"total_supply": 100, "max_supply": 100}])
    tracker = make_tracker(
        db, inspector, FakeEditor(), on_signal=recorder, near_sellout_threshold=0.9
    )
    await tracker.tick()

    assert recorder.signals == []
    await db.close()


async def test_near_sellout_alert_fires_past_the_threshold(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    recorder = SignalRecorder()
    inspector = ScriptedInspector([{"total_supply": 95, "max_supply": 100}])
    tracker = make_tracker(
        db, inspector, FakeEditor(), on_signal=recorder, near_sellout_threshold=0.9
    )
    await tracker.tick()

    assert len(recorder.signals) == 1
    signal = recorder.signals[0]
    assert signal.kind == "mint_near_sellout"
    assert signal.contract_address == NFT
    assert "95%" in signal.text
    await db.close()


async def test_near_sellout_alert_does_not_fire_below_the_threshold(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    recorder = SignalRecorder()
    inspector = ScriptedInspector([{"total_supply": 50, "max_supply": 100}])
    tracker = make_tracker(
        db, inspector, FakeEditor(), on_signal=recorder, near_sellout_threshold=0.9
    )
    await tracker.tick()

    assert recorder.signals == []
    await db.close()


async def test_near_sellout_alert_fires_only_once_per_project(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    recorder = SignalRecorder()
    inspector = ScriptedInspector(
        [{"total_supply": 95, "max_supply": 100}, {"total_supply": 96, "max_supply": 100}]
    )
    tracker = make_tracker(
        db, inspector, FakeEditor(), on_signal=recorder, near_sellout_threshold=0.9
    )
    await tracker.tick()
    await tracker.tick()

    assert len(recorder.signals) == 1
    await db.close()


async def test_near_sellout_alert_is_a_noop_without_on_signal_configured(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    inspector = ScriptedInspector([{"total_supply": 95, "max_supply": 100}])
    tracker = make_tracker(db, inspector, FakeEditor())  # on_signal не задан
    await tracker.tick()  # не должно упасть
    await db.close()


NFT2 = "0x" + "b" * 40


async def test_tick_pauses_between_projects_to_avoid_bursting_the_rpc(tmp_path, monkeypatch):
    # Раньше тик перебирал все отслеживаемые проекты залпом, без единой
    # паузы между ними. При десятке проектов это десятки RPC-вызовов подряд
    # за долю секунды — публичные ноды режут такое как всплеск (429), и
    # часть карточек в этом тике молча оставалась без обновления.
    import wlparser.tracking.service as service_module

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db, address=NFT)
    await seed_project(db, address=NFT2)
    await db.start_tracking(NFT)
    await db.start_tracking(NFT2)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10}])
    tracker = make_tracker(db, inspector, editor, check_delay_s=0.5)
    await tracker.tick()

    assert sleeps == [0.5]  # пауза перед вторым проектом, не перед первым
    assert len(editor.edits) == 2
    await db.close()


async def test_stale_tracking_stops_after_configured_checks(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    inspector = ScriptedInspector([{"total_supply": 10}] * 5)
    tracker = make_tracker(db, inspector, FakeEditor(), stale_after_checks=3)
    for _ in range(4):
        await tracker.tick()

    project = await db.get_project(NFT)
    assert project["tracking_status"] == "stale"
    # Первый тик даёт правку (нет предыдущего снимка), дальше три тика подряд
    # без изменений добирают порог до stale.
    assert project["stale_checks"] == 3
    await db.close()


class FakeNow:
    """Текущий момент, который тест двигает вручную между тиками.

    В отличие от конечного итератора: self._now() может вызываться внутри
    одного тика больше одного раза (сам tick() и потом _check_one), и все
    эти вызовы должны видеть один и тот же "текущий момент", а не по
    очереди съедать значения из списка.
    """

    def __init__(self, start: datetime):
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


async def test_low_fill_project_is_checked_far_less_often(tmp_path):
    # Заявка владельца: "если там 1-2 процента, то вообще похуй" — раз в
    # обычные пять минут для такой коллекции незачем, лишняя нагрузка на
    # RPC/OpenSea без всякой пользы.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 1, "max_supply": 100}])  # 1%
    now = FakeNow(datetime.now(timezone.utc))
    tracker = make_tracker(
        db, inspector, editor, refresh_interval_s=300.0, now=now
    )
    await tracker.tick()
    now.advance(minutes=6)  # больше обычных 5 минут, но при 1% интервал — час
    await tracker.tick()

    assert len(editor.edits) == 1  # второй тик пропущен — этому проекту ещё рано
    await db.close()


async def test_high_fill_project_is_checked_more_often(tmp_path):
    # И наоборот: "коллекции который подходят к распродаже обновляй чаще".
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector(
        [{"total_supply": 95, "max_supply": 100}, {"total_supply": 96, "max_supply": 100}]
    )
    now = FakeNow(datetime.now(timezone.utc))
    tracker = make_tracker(
        db, inspector, editor, refresh_interval_s=300.0, now=now
    )
    await tracker.tick()
    now.advance(minutes=2)  # меньше обычных 5 минут, но у порога распродажи — как раз пора
    await tracker.tick()

    assert len(editor.edits) == 2  # проверился и во второй раз
    await db.close()


async def test_low_fill_project_is_eventually_checked_after_enough_time(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector(
        [{"total_supply": 1, "max_supply": 100}, {"total_supply": 2, "max_supply": 100}]
    )
    now = FakeNow(datetime.now(timezone.utc))
    tracker = make_tracker(
        db, inspector, editor, refresh_interval_s=300.0, now=now
    )
    await tracker.tick()
    now.advance(hours=2)  # дольше часового интервала для 1%
    await tracker.tick()

    assert len(editor.edits) == 2
    await db.close()


async def test_expired_tracking_stops_without_editing(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)
    # Искусственно состарим начало слежения.
    old = (T0 - timedelta(hours=100)).isoformat()
    await db._conn.execute(
        "UPDATE projects SET tracking_started_at = ? WHERE address = ?", (old, NFT)
    )
    await db._conn.commit()

    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), editor, max_duration_hours=48
    )
    await tracker.tick()

    assert editor.edits == []
    assert (await db.get_project(NFT))["tracking_status"] == "expired"
    await db.close()


async def test_deleted_message_removes_the_thread(tmp_path):
    from wlparser.notify.tracking import LiveCardEditor

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    class GoneBot:
        async def edit_message_text(self, **kwargs):
            raise RuntimeError("Bad Request: message to edit not found")

    real_editor = LiveCardEditor(GoneBot(), on_message_gone=db.delete_thread)
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 10}]), real_editor)
    await tracker.tick()

    assert await db.threads_for_address(NFT) == []
    await db.close()


async def test_interest_score_is_recomputed_each_tick(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    inspector = ScriptedInspector([{"total_supply": 95, "max_supply": 100}])
    tracker = make_tracker(db, inspector, FakeEditor())
    await tracker.tick()

    project = await db.get_project(NFT)
    assert project["interest_score"] is not None
    assert project["interest_score"] > 0
    await db.close()


async def test_opensea_floor_is_included_when_slug_known(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": 12}})
    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), editor, opensea=opensea
    )
    await tracker.tick()

    assert "0.05 ETH" in editor.edits[0][3]
    assert "12" in editor.edits[0][3]
    await db.close()


async def test_periodic_tick_includes_recent_sales_too(tmp_path):
    # Раньше последнюю/макс. продажу показывала только команда /find —
    # обычная плановая карточка про них молчала, хотя объём и флор
    # обновлялись как обычно. Разница была не по замыслу, а по недосмотру.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
        sales={"cash-bears": {
            "last_sale": 0.3, "last_sale_symbol": "ETH",
            "highest_sale": 0.6, "highest_sale_symbol": "ETH",
        }},
    )
    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), editor, opensea=opensea
    )
    await tracker.tick()

    assert "0.3 ETH" in editor.edits[0][3]
    assert "0.6 ETH" in editor.edits[0][3]
    await db.close()


async def test_recent_sales_are_cached_within_the_ttl(tmp_path):
    # Раньше карточка спрашивала последнюю/макс. продажу у OpenSea на
    # КАЖДЫЙ тик КАЖДОГО проекта — вместе с уже существующим запросом
    # floor/объёма это удваивало нагрузку на лимит частоты OpenSea и на
    # практике часть карточек вообще переставала показывать эти поля.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
        sales={"cash-bears": {"last_sale": 0.3, "last_sale_symbol": "ETH"}},
    )
    clock = iter([0.0, 100.0, 200.0]).__next__
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(),
        opensea=opensea, sales_cache_ttl_s=900.0, clock=clock,
    )
    await tracker.tick()
    await tracker.tick()

    assert opensea.sales_calls == 1  # второй тик попал в кеш
    await db.close()


async def test_recent_sales_refetch_after_the_cache_expires(tmp_path):
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    # Соцсети уже известны — иначе бэкафилл (см. _backfill_links) тоже
    # расходовал бы значения из clock, и тест перестал бы быть про кеш продаж.
    await db.save_links(NFT, ProjectLinks(website="https://example.com"))
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
        sales={"cash-bears": {"last_sale": 0.3, "last_sale_symbol": "ETH"}},
    )
    clock = iter([0.0, 1000.0, 2000.0]).__next__  # дальше, чем ttl=900
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(),
        opensea=opensea, sales_cache_ttl_s=900.0, clock=clock,
    )
    await tracker.tick()
    await tracker.tick()

    assert opensea.sales_calls == 2
    await db.close()


async def test_find_always_bypasses_the_sales_cache(tmp_path):
    # /find — разовый запрос по требованию, а не непрерывный опрос: там
    # нет причины экономить на свежести данных.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
        sales={"cash-bears": {"last_sale": 0.3, "last_sale_symbol": "ETH"}},
    )
    project = await db.get_project(NFT)
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(),
        opensea=opensea, clock=lambda: 0.0,
    )
    await tracker.render_card(project, recent_sales=True)
    await tracker.render_card(project, recent_sales=True)

    assert opensea.sales_calls == 2
    await db.close()


async def test_slug_is_discovered_lazily_when_missing(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)  # без opensea_slug

    opensea = FakeOpenSea(slug="cash-bears", stats={"cash-bears": {"floor_price": 0.1, "floor_symbol": "ETH"}})
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(), opensea=opensea
    )
    await tracker.tick()

    assert (await db.get_project(NFT))["opensea_slug"] == "cash-bears"
    await db.close()


async def test_links_are_backfilled_once_slug_is_known(tmp_path):
    # До этой правки website/twitter/discord/telegram пробовались получить
    # ровно один раз — в момент самого первого алерта, когда OpenSea чаще
    # всего ещё не проиндексировал совсем новый контракт. Если тогда не
    # получилось, эти поля оставались NULL навсегда, даже когда slug давно
    # найден и OpenSea прекрасно всё знает.
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
        links={"cash-bears": ProjectLinks(website="https://cashbears.example")},
    )
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(), opensea=opensea)
    await tracker.tick()

    assert (await db.get_project(NFT))["website"] == "https://cashbears.example"
    await db.close()


async def test_backfill_is_skipped_once_socials_are_already_known(tmp_path):
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.save_links(NFT, ProjectLinks(website="https://already-known.example"))
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
    )
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(), opensea=opensea)
    await tracker.tick()

    assert opensea.links_calls == 0
    await db.close()


async def test_backfill_retries_on_a_later_tick_when_still_unknown(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
    )
    # Растёт без конца, а не конечный список: тик расходует clock() и на
    # бэкафилл ссылок, и на кеш продаж, и точное число вызовов — деталь
    # реализации, а не то, что этот тест проверяет.
    state = {"t": 0.0}

    def ever_increasing_clock():
        state["t"] += 2000.0  # дальше, чем ttl=900 по умолчанию — каждый раз "протухло"
        return state["t"]

    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(),
        opensea=opensea, clock=ever_increasing_clock,
    )
    await tracker.tick()
    await tracker.tick()

    assert opensea.links_calls == 2  # не нашли раньше — пробуем снова
    await db.close()


async def test_backfilled_website_feeds_the_mint_price_fallback_same_tick(tmp_path):
    # Закрывает ровно ту жалобу: цена с сайта не показывалась, потому что
    # website почти всегда оставался NULL из-за одноразовой попытки при
    # самом первом алерте. Теперь бэкафилл должен успеть до рендера той же
    # самой проверки, а не только со следующего тика.
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db, mint_price_wei=None)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(
        stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}},
        links={"cash-bears": ProjectLinks(website="https://cashbears.example")},
    )
    website = FakeWebsite(price=0.09)
    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10, "mint_price_wei": None}])
    tracker = make_tracker(db, inspector, editor, opensea=opensea, website=website)
    await tracker.tick()

    assert website.calls == ["https://cashbears.example"]
    assert "0.09 ETH (с сайта)" in editor.edits[0][3]
    await db.close()


async def test_without_opensea_price_and_supply_still_work(tmp_path):
    # Основная часть просьбы — цена и прогресс минта — не должна зависеть
    # от того, настроен ли ключ OpenSea.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 30}]), editor, opensea=None
    )
    await tracker.tick()

    assert "30 / 100" in editor.edits[0][3]
    assert "флор" not in editor.edits[0][3]
    await db.close()


class FakeWebsite:
    def __init__(self, price=0.07):
        self.price = price
        self.calls = []

    async def mint_price_eth(self, url):
        self.calls.append(url)
        return self.price


async def test_website_price_fills_in_when_chain_detection_finds_nothing(tmp_path):
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db, mint_price_wei=None)
    await db.save_links(NFT, ProjectLinks(website="https://example.com"))
    await db.start_tracking(NFT)

    editor = FakeEditor()
    website = FakeWebsite(price=0.07)
    inspector = ScriptedInspector([{"total_supply": 10, "mint_price_wei": None}])
    tracker = make_tracker(db, inspector, editor, website=website)
    await tracker.tick()

    assert "0.07 ETH (с сайта)" in editor.edits[0][3]
    assert website.calls == ["https://example.com"]
    await db.close()


async def test_website_price_is_not_consulted_when_chain_already_knows(tmp_path):
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.save_links(NFT, ProjectLinks(website="https://example.com"))
    await db.start_tracking(NFT)

    editor = FakeEditor()
    website = FakeWebsite(price=0.07)
    inspector = ScriptedInspector([{"total_supply": 10, "mint_price_wei": 10**16}])
    tracker = make_tracker(db, inspector, editor, website=website)
    await tracker.tick()

    assert website.calls == []
    assert "(с сайта)" not in editor.edits[0][3]
    await db.close()


async def test_website_price_is_cached_within_the_ttl(tmp_path):
    from wlparser.enrich.models import ProjectLinks

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db, mint_price_wei=None)
    await db.save_links(NFT, ProjectLinks(website="https://example.com"))
    await db.start_tracking(NFT)

    website = FakeWebsite(price=0.07)
    clock = iter([0.0, 100.0]).__next__  # оба тика внутри ttl=3600 по умолчанию
    inspector = ScriptedInspector([{"total_supply": 10, "mint_price_wei": None}])
    tracker = make_tracker(db, inspector, FakeEditor(), website=website, clock=clock)
    await tracker.tick()
    await tracker.tick()

    assert len(website.calls) == 1
    await db.close()


async def test_pricing_line_is_included_when_pricing_is_configured(tmp_path):
    class FakePricing:
        async def usd_line(self, mint_price_wei):
            return f"${mint_price_wei / 10**16:.2f}"

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10, "mint_price_wei": 10**16}])
    tracker = make_tracker(db, inspector, editor, pricing=FakePricing())
    await tracker.tick()

    assert "$1.00" in editor.edits[0][3]
    await db.close()


async def test_floor_gets_usd_estimate_when_symbol_is_eth(tmp_path):
    class FakePricing:
        async def usd_amount(self, amount_eth):
            return f"${amount_eth * 2000:.2f}"

        async def usd_line(self, mint_price_wei):
            return None

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "ETH", "num_owners": None}})
    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), editor,
        opensea=opensea, pricing=FakePricing(),
    )
    await tracker.tick()

    assert "$100.00" in editor.edits[0][3]
    await db.close()


async def test_floor_gets_no_usd_estimate_for_an_unknown_currency(tmp_path):
    # Курс с CoinGecko — ETH/USD. Подставлять его к произвольному ERC-20,
    # чей курс к доллару нам неизвестен, значило бы его выдумать.
    class FakePricing:
        async def usd_amount(self, amount_eth):
            return f"${amount_eth * 2000:.2f}"

        async def usd_line(self, mint_price_wei):
            return None

    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(stats={"cash-bears": {"floor_price": 0.05, "floor_symbol": "PEPE", "num_owners": None}})
    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), editor,
        opensea=opensea, pricing=FakePricing(),
    )
    await tracker.tick()

    assert "$" not in editor.edits[0][3]
    await db.close()


async def test_floor_in_a_stablecoin_converts_one_to_one_without_a_rate(tmp_path):
    # USDG и подобные привязаны к доллару по определению — курс им не
    # нужен, поэтому конвертация работает, даже если pricing не настроен.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")
    await db.start_tracking(NFT)

    opensea = FakeOpenSea(stats={"cash-bears": {"floor_price": 0.69, "floor_symbol": "USDG", "num_owners": None}})
    editor = FakeEditor()
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), editor,
        opensea=opensea, pricing=None,
    )
    await tracker.tick()

    assert "0.69 USDG (≈ $0.69)" in editor.edits[0][3]
    await db.close()


async def test_no_pricing_configured_means_no_usd_line(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    editor = FakeEditor()
    inspector = ScriptedInspector([{"total_supply": 10, "mint_price_wei": 10**16}])
    tracker = make_tracker(db, inspector, editor, pricing=None)
    await tracker.tick()

    assert "$" not in editor.edits[0][3]
    await db.close()


# --- render_card (используется командой /find) ---


async def test_render_card_shows_current_state_without_touching_snapshots(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.start_tracking(NFT)

    project = await db.get_project(NFT)
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 25}]), FakeEditor())
    text = await tracker.render_card(project)

    assert "25 / 100" in text
    assert await db.snapshot_history(NFT) == []  # разовый взгляд, не тик
    await db.close()


async def test_render_card_includes_recent_sales_when_requested(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)
    await db.set_opensea_slug(NFT, "cash-bears")

    opensea = FakeOpenSea(
        stats={"cash-bears": {
            "floor_price": 0.05, "floor_symbol": "ETH", "num_owners": 10,
            "total_volume": 1.0, "volume_symbol": "ETH", "total_sales": 5, "average_price": 0.2,
        }},
    )
    opensea.recent_sales = {"cash-bears": {
        "last_sale": 0.3, "last_sale_symbol": "ETH",
        "highest_sale": 0.6, "highest_sale_symbol": "ETH", "sample_size": 5,
    }}

    async def collection_recent_sales(slug):
        return opensea.recent_sales.get(slug)

    opensea.collection_recent_sales = collection_recent_sales

    project = await db.get_project(NFT)
    tracker = make_tracker(
        db, ScriptedInspector([{"total_supply": 10}]), FakeEditor(), opensea=opensea
    )
    text = await tracker.render_card(project, recent_sales=True)

    assert "0.3 ETH" in text
    assert "0.6 ETH" in text
    await db.close()


async def test_render_card_without_recent_sales_omits_that_section(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)

    project = await db.get_project(NFT)
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 10}]), FakeEditor())
    text = await tracker.render_card(project, recent_sales=False)

    assert "последняя продажа" not in text
    await db.close()


async def test_track_performs_an_immediate_first_check(tmp_path):
    # Раньше первое сообщение о проекте застревало в виде одноразового
    # алерта до пяти минут — до ближайшего планового тика. track() теперь
    # правит его сразу же, а не ждёт расписания.
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)

    editor = FakeEditor()
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 15}]), editor)
    await tracker.track(NFT)

    assert len(editor.edits) == 1
    assert "15 / 100" in editor.edits[0][3]
    await db.close()


async def test_track_called_twice_does_not_double_check(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    await seed_project(db)

    editor = FakeEditor()
    tracker = make_tracker(db, ScriptedInspector([{"total_supply": 15}]), editor)
    await tracker.track(NFT)
    await tracker.track(NFT)  # уже отслеживается — повторный вызов не должен ничего делать

    assert len(editor.edits) == 1
    await db.close()
