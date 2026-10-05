import asyncio
from datetime import datetime, timezone

from wlparser.models import Signal
from wlparser.pipeline.runner import Pipeline, default_score

NFT = "0x" + "a" * 40


def signal(kind="mint_surge", address=NFT):
    return Signal(
        source="onchain",
        kind=kind,
        text="t",
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw={},
    )


class FakeDb:
    def __init__(self):
        self.recorded = []
        self.seen = set()

    async def mark_seen(self, key):
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    async def record_signal(self, sig, score):
        self.recorded.append((sig, score))


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, sig):
        self.sent.append(sig)


async def drain(pipeline, queue):
    """Прогоняет очередь до конца и останавливает бесконечный цикл.

    Также ждёт фоновые задачи запуска слежения: они не блокируют очередь
    (в этом их смысл), но тесты должны видеть их результат до assert'ов.
    """
    task = asyncio.create_task(pipeline.run(queue))
    await queue.join()
    await pipeline.wait_for_tracking()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_records_and_notifies_urgent_signal():
    db, notifier = FakeDb(), FakeNotifier()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert len(db.recorded) == 1
    assert len(notifier.sent) == 1


async def test_non_urgent_signal_is_stored_but_not_sent_instantly():
    # mint_started и твиттер копятся в базе и уходят в периодический
    # дайджест (см. digest.py), а не мгновенным алертом — только
    # mint_surge/mint_near_sellout будят подписчиков сразу.
    db, notifier = FakeDb(), FakeNotifier()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45)
    queue = asyncio.Queue()
    await queue.put(signal(kind="mint_started"))
    await drain(pipeline, queue)
    assert len(db.recorded) == 1
    assert notifier.sent == []


async def test_duplicate_signal_is_dropped():
    db, notifier = FakeDb(), FakeNotifier()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45)
    queue = asyncio.Queue()
    await queue.put(signal())
    await queue.put(signal())
    await drain(pipeline, queue)
    assert len(notifier.sent) == 1


async def test_low_score_is_stored_but_not_notified():
    db, notifier = FakeDb(), FakeNotifier()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 10, min_score=45)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert len(db.recorded) == 1
    assert notifier.sent == []


async def test_notifier_failure_does_not_stop_pipeline():
    class Broken(FakeNotifier):
        async def send(self, sig):
            raise RuntimeError("telegram down")

    db = FakeDb()
    pipeline = Pipeline(db, Broken(), score_fn=lambda s: 90, min_score=45)
    queue = asyncio.Queue()
    await queue.put(signal(address="0x" + "b" * 40))
    await queue.put(signal(address="0x" + "c" * 40))
    await drain(pipeline, queue)
    assert len(db.recorded) == 2


def test_default_score_ranks_onchain_facts_above_chatter():
    assert default_score(signal(kind="mint_surge")) > default_score(
        signal(kind="contract_deployed")
    )
    assert default_score(signal(kind="contract_deployed")) > default_score(
        signal(kind="chatter")
    )


class BlockingNoise:
    """Фильтр, который придерживает всё."""

    def __init__(self):
        self.seen = []

    def allow(self, signal, score, now):
        from wlparser.pipeline.noise import Decision

        self.seen.append(signal)
        return Decision(False, "тест")


async def test_held_back_signal_is_still_stored():
    # Фильтр решает, о чём будить, а не что запоминать.
    db, notifier, noise = FakeDb(), FakeNotifier(), BlockingNoise()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, noise=noise)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert len(db.recorded) == 1
    assert notifier.sent == []
    assert len(noise.seen) == 1


async def test_noise_filter_is_not_consulted_below_the_score_threshold():
    db, notifier, noise = FakeDb(), FakeNotifier(), BlockingNoise()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 10, min_score=45, noise=noise)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert noise.seen == []


class FakeQuality:
    def __init__(self, verdict=True):
        self.verdict = verdict
        self.seen = []

    async def allow(self, signal):
        self.seen.append(signal)
        return self.verdict


async def test_quality_filter_can_suppress_a_signal():
    db, notifier, quality = FakeDb(), FakeNotifier(), FakeQuality(verdict=False)
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, quality=quality)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert len(db.recorded) == 1
    assert notifier.sent == []
    assert len(quality.seen) == 1


async def test_quality_filter_is_consulted_after_noise_passes():
    db, notifier = FakeDb(), FakeNotifier()
    quality = FakeQuality(verdict=True)
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, quality=quality)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert len(notifier.sent) == 1
    assert len(quality.seen) == 1


async def test_quality_filter_is_not_consulted_when_noise_already_held_it_back():
    db, notifier, noise = FakeDb(), FakeNotifier(), BlockingNoise()
    quality = FakeQuality(verdict=True)
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, noise=noise, quality=quality
    )
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert quality.seen == []


class RecordingDelivered:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, address):
        self.calls.append(address)
        if self.fail:
            raise RuntimeError("tracker exploded")


async def test_on_delivered_is_called_for_every_signal_with_an_address():
    # Слежение стартует для каждого найденного контракта — сбор истории
    # не зависит от того, достаточно ли сигнал интересен для мгновенного
    # алерта (см. test_on_delivered_fires_even_for_non_urgent_signals).
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingDelivered()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, on_delivered=hook)
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert hook.calls == [NFT]


async def test_on_delivered_fires_even_for_non_urgent_signals():
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingDelivered()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, on_delivered=hook)
    queue = asyncio.Queue()
    await queue.put(signal(kind="mint_started"))
    await drain(pipeline, queue)
    assert hook.calls == [NFT]
    assert notifier.sent == []  # мгновенного алерта не было, а слежение всё равно стартовало


async def test_on_delivered_fires_even_when_the_noise_filter_holds_the_signal():
    # Сбор данных отделён от решения "будить или нет" — held-back сигнал
    # всё равно должен запустить слежение за проектом.
    db, notifier, noise = FakeDb(), FakeNotifier(), BlockingNoise()
    hook = RecordingDelivered()
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, noise=noise, on_delivered=hook
    )
    queue = asyncio.Queue()
    await queue.put(signal())
    await drain(pipeline, queue)
    assert hook.calls == [NFT]


async def test_on_delivered_is_not_called_without_a_contract_address():
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingDelivered()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, on_delivered=hook)
    queue = asyncio.Queue()
    await queue.put(signal(address=None))
    await drain(pipeline, queue)
    assert hook.calls == []


async def test_on_delivered_does_not_block_the_next_signal():
    # Раньше запуск слежения ждался прямо в очереди: если первая проверка
    # нового проекта застревала в повторных попытках RPC (см. chain/rpc.py,
    # там паузы по 20-55 с при лимите 429), вся очередь алертов стояла
    # позади неё. Теперь это фоновая задача — следующий сигнал не должен
    # её дожидаться.
    slow_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_hook(address):
        slow_started.set()
        await release.wait()

    db, notifier = FakeDb(), FakeNotifier()
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, on_delivered=slow_hook)
    queue = asyncio.Queue()
    task = asyncio.create_task(pipeline.run(queue))

    await queue.put(signal(address="0x" + "d" * 40))
    await asyncio.wait_for(slow_started.wait(), timeout=1)

    await queue.put(signal(address="0x" + "e" * 40))
    await asyncio.wait_for(queue.join(), timeout=1)  # не должно виснуть на первом

    assert len(notifier.sent) == 2
    release.set()
    await pipeline.wait_for_tracking()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_on_delivered_failure_does_not_break_the_pipeline():
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingDelivered(fail=True)
    pipeline = Pipeline(db, notifier, score_fn=lambda s: 90, min_score=45, on_delivered=hook)
    queue = asyncio.Queue()
    await queue.put(signal(address="0x" + "d" * 40))
    await queue.put(signal(address="0x" + "e" * 40))
    await drain(pipeline, queue)
    assert len(notifier.sent) == 2  # сбой слежения не мешает доставке алертов


class RecordingSignalHook:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, sig):
        self.calls.append(sig)
        if self.fail:
            raise RuntimeError("poster exploded")


async def test_on_signal_delivered_receives_the_full_signal():
    # В отличие от on_delivered (только адрес), черновику поста в X нужен
    # весь сигнал — source и kind решают, стоит ли вообще постить.
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingSignalHook()
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, on_signal_delivered=hook
    )
    queue = asyncio.Queue()
    sig = signal()
    await queue.put(sig)
    await drain(pipeline, queue)
    assert hook.calls == [sig]


async def test_on_signal_delivered_fires_for_non_urgent_signals_too():
    # Черновик поста в X не завязан на мгновенный алерт — mint_started
    # не шлётся мгновенно, но всё равно может стать черновиком.
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingSignalHook()
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, on_signal_delivered=hook
    )
    queue = asyncio.Queue()
    sig = signal(kind="mint_started")
    await queue.put(sig)
    await drain(pipeline, queue)
    assert hook.calls == [sig]
    assert notifier.sent == []


async def test_on_signal_delivered_fires_even_when_the_quality_filter_holds_the_alert():
    # quality.py придерживает только УВЕДОМЛЕНИЕ по собственному контракту —
    # у постинга/цитирования свой порог (caption.py::SKIP_TOKEN). Раньше хук
    # стоял после этой проверки, так что типичный шаблонный wl_announced
    # твит, забракованный тут как "шум", никогда не доходил до своей же
    # оценки для цитирования — отсюда нулевые цитирования в проде.
    db, notifier, quality = FakeDb(), FakeNotifier(), FakeQuality(verdict=False)
    hook = RecordingSignalHook()
    pipeline = Pipeline(
        db,
        notifier,
        score_fn=lambda s: 90,
        min_score=45,
        quality=quality,
        on_signal_delivered=hook,
    )
    queue = asyncio.Queue()
    sig = signal()
    await queue.put(sig)
    await drain(pipeline, queue)
    assert hook.calls == [sig]
    assert notifier.sent == []  # мгновенный алерт всё равно придержан


async def test_on_signal_delivered_fires_for_onchain_even_when_the_contract_cooldown_holds_it():
    # Реальный случай из прода: "начался минт" прошёл фильтр и занял
    # topic_key контракта на 30 минут, а следующий за ним "всплеск" для
    # ТОГО ЖЕ контракта — совсем другая, самостоятельно постабельная
    # новость — целиком придерживался этим кулдауном, так и не дойдя до
    # своей собственной проверки на постабельность. mint_surge физически
    # не повторяется для контракта на уровне источника (onchain.py), так
    # что кулдаун здесь защищает только алерт, не постинг.
    from wlparser.pipeline.noise import NoiseFilter

    db, notifier = FakeDb(), FakeNotifier()
    noise = NoiseFilter(onchain_topic_cooldown_minutes=30.0)
    hook = RecordingSignalHook()
    pipeline = Pipeline(
        db,
        notifier,
        score_fn=lambda s: 95,
        min_score=45,
        noise=noise,
        on_signal_delivered=hook,
    )
    queue = asyncio.Queue()
    await queue.put(signal(kind="mint_started"))
    await queue.put(signal(kind="mint_surge"))
    await drain(pipeline, queue)

    assert [sig.kind for sig in hook.calls] == ["mint_started", "mint_surge"]
    # Сам алерт на второй сигнал (повтор по контракту) всё же придержан,
    # как и раньше — меняется только то, что постинг его больше не теряет.
    assert notifier.sent == []


async def test_on_signal_delivered_fires_even_when_the_urgent_notify_fails():
    class Broken(FakeNotifier):
        async def send(self, sig):
            raise RuntimeError("telegram down")

    db = FakeDb()
    hook = RecordingSignalHook()
    pipeline = Pipeline(
        db, Broken(), score_fn=lambda s: 90, min_score=45, on_signal_delivered=hook
    )
    queue = asyncio.Queue()
    sig = signal()
    await queue.put(sig)
    await drain(pipeline, queue)
    assert hook.calls == [sig]


async def test_on_signal_delivered_failure_does_not_break_the_pipeline():
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingSignalHook(fail=True)
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, on_signal_delivered=hook
    )
    queue = asyncio.Queue()
    await queue.put(signal(address="0x" + "d" * 40))
    await queue.put(signal(address="0x" + "e" * 40))
    await drain(pipeline, queue)
    assert len(notifier.sent) == 2


async def test_on_signal_delivered_works_without_a_contract_address():
    # Твит-worthy сигнал всегда он-чейн и с адресом на практике, но хук сам
    # по себе не должен требовать его — это отдельная гарантия от on_delivered.
    db, notifier = FakeDb(), FakeNotifier()
    hook = RecordingSignalHook()
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, on_signal_delivered=hook
    )
    queue = asyncio.Queue()
    await queue.put(signal(address=None))
    await drain(pipeline, queue)
    assert len(hook.calls) == 1


async def test_urgent_kinds_are_configurable():
    db, notifier = FakeDb(), FakeNotifier()
    pipeline = Pipeline(
        db, notifier, score_fn=lambda s: 90, min_score=45, urgent_kinds=frozenset({"chatter"})
    )
    queue = asyncio.Queue()
    await queue.put(signal(kind="mint_surge"))  # больше не срочный по этой настройке
    await queue.put(signal(kind="chatter", address="0x" + "f" * 40))
    await drain(pipeline, queue)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].kind == "chatter"
