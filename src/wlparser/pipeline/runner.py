"""Разбор общей очереди сигналов: дедуп → запись → уведомление."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..models import Signal

log = logging.getLogger(__name__)

# Временные веса до появления настоящего скорера на этапе 4.
# Он-чейн события стоят дороже соцсетевых, потому что это факты, а не слухи.
_BASE_SCORES = {
    "mint_surge": 95,
    "mint_near_sellout": 95,
    "mint_started": 85,
    "contract_deployed": 70,
    "wl_announced": 50,
    "chatter": 20,
}


def default_score(signal: Signal) -> int:
    return _BASE_SCORES.get(signal.kind, 30)


# Только эти два кайнда шлются мгновенным алертом всем подписчикам — WL
# может закрыться за минуты, ждать нельзя. Всё остальное (mint_started,
# твиттер) копится в базе и уходит в периодический дайджест (см. digest.py) —
# владелец сам попросил не будить его на каждую находку. Черновик поста в X
# (on_signal_delivered) и слежение (on_delivered) на эту границу не
# оглядываются — публикация и сбор данных не должны зависеть от того, шлём
# ли мы мгновенное уведомление именно сейчас.
URGENT_KINDS = frozenset({"mint_surge", "mint_near_sellout"})


class Pipeline:
    def __init__(
        self,
        db,
        notifier,
        *,
        score_fn=default_score,
        min_score: int = 45,
        noise=None,
        quality=None,
        on_delivered=None,
        on_signal_delivered=None,
        urgent_kinds=URGENT_KINDS,
    ):
        self._db = db
        self._notifier = notifier
        self._score_fn = score_fn
        self._min_score = min_score
        self._noise = noise
        # Финальная проверка ИИ поверх структурных фильтров — см. quality.py.
        self._quality = quality
        # Вызывается для КАЖДОГО сигнала с адресом контракта, а не только
        # для мгновенно уведомлённых — слежение теперь собирает данные по
        # всем найденным проектам, а не только по тем, что показались
        # достаточно интересными для мгновенного алерта.
        self._on_delivered = on_delivered
        # Отдельный хук на весь сигнал (не только адрес) — например, черновик
        # поста в X решает по kind/source, а не только по контракту.
        self._on_signal_delivered = on_signal_delivered
        self._urgent_kinds = urgent_kinds
        # Фоновые задачи живут отдельно от очереди сигналов: у них своя
        # ссылка, чтобы сборщик мусора их не подобрал, пока они летят.
        self._background_tasks: set[asyncio.Task] = set()

    async def run(self, queue: "asyncio.Queue[Signal]") -> None:
        while True:
            signal = await queue.get()
            try:
                await self._handle(signal)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сбой обработки сигнала %s", signal.dedupe_key)
            finally:
                queue.task_done()

    async def _handle(self, signal: Signal) -> None:
        if not await self._db.mark_seen(signal.dedupe_key):
            log.debug("дубликат отброшен: %s", signal.dedupe_key)
            return

        score = self._score_fn(signal)
        await self._db.record_signal(signal, score)

        if self._on_delivered is not None and signal.contract_address:
            # Стартует для КАЖДОГО контракта, независимо от порога/шума/ИИ
            # ниже — сбор истории отделён от решения, о чём будить. В фоне,
            # не в очереди: первая проверка нового проекта делает несколько
            # запросов к RPC и может застрять на минуту-другую, если сеть в
            # этот момент режет частоту (см. chain/rpc.py). Если ждать её
            # здесь, вся очередь встанет позади одного невезучего контракта.
            self._spawn(self._track(signal.contract_address), label=signal.contract_address)

        if score < self._min_score:
            log.debug("ниже порога (%d), только в базу: %s", score, signal.text[:60])
            return

        # Сигнал уже сохранён: фильтр решает, о чём будить, а не что помнить.
        noise_ok = True
        if self._noise is not None:
            verdict = self._noise.allow(signal, score, datetime.now(timezone.utc))
            noise_ok = verdict.allowed
            if not noise_ok:
                log.info("придержано (%s): %s", verdict.reason, signal.text[:70])

        # Постинг/цитирование для он-чейн сигналов не должно зависеть от
        # кулдауна по контракту (noise.py) — он задуман против повторного
        # АЛЕРТА про одно и то же ("начался минт" и "всплеск" — по сути
        # пересказ), но mint_surge/mint_near_sellout и так физически не
        # повторяются для одного контракта на уровне самого источника (см.
        # sources/onchain.py::_surged, tracking/service.py::near_sellout_alerted)
        # — так что для них это не защита от дубля, а баг: реальный, крупный
        # всплеск после недавнего "начался минт" по тому же контракту
        # придерживался целиком, вообще не доходя до своей же проверки на
        # постабельность. Для твиттера (цитирование wl_announced) кулдаун,
        # наоборот, нужен как есть — там дубли/шаблонный хайп по теме
        # реально случаются, и одноразовой гарантии от источника нет.
        if self._on_signal_delivered is not None and (
            noise_ok or signal.source == "onchain"
        ):
            self._spawn(self._notify_signal(signal), label=signal.dedupe_key)

        if not noise_ok:
            return

        # ИИ-фильтр шума (quality.py) по собственному контракту придерживает
        # только УВЕДОМЛЕНИЕ, не постинг/цитирование — он настроен на вопрос
        # "стоит ли будить владельца", с заведомым перекосом в сторону
        # пропуска (YES при любой неуверенности), и blocks именно то, что
        # похоже на шаблонный хайп — а это ровно профиль типичного
        # wl_announced твита, который maybe_quote мог бы процитировать. У
        # постинга/цитирования теперь свой собственный, отдельно откалиброванный
        # порог (caption.py::SKIP_TOKEN) — пропускать через ещё один чужой
        # фильтр до него было ошибкой, из-за которой цитирование почти
        # никогда не доходило до своей же проверки.
        quality_ok = True
        if self._quality is not None:
            quality_ok = await self._quality.allow(signal)
            if not quality_ok:
                log.info("похоже на шум по мнению ИИ, не будим владельца: %s", signal.text[:70])

        if signal.kind in self._urgent_kinds and quality_ok:
            try:
                await self._notifier.send(signal)
            except Exception:
                log.exception("уведомление не ушло, сигнал уже сохранён в базе")

    def _spawn(self, coro, *, label: str) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _track(self, address: str) -> None:
        try:
            await self._on_delivered(address)
        except Exception:
            log.exception("не удалось запустить слежение за %s", address)

    async def _notify_signal(self, signal: Signal) -> None:
        try:
            await self._on_signal_delivered(signal)
        except Exception:
            log.exception("не удалось обработать доставленный сигнал %s", signal.dedupe_key)

    async def wait_for_tracking(self) -> None:
        """Ждёт все ещё не завершившиеся фоновые задачи (слежение, черновики постов).

        Только для тестов и аккуратного завершения: обычная работа бота
        не должна ждать эти задачи, в этом весь смысл фонового запуска.
        """
        pending = list(self._background_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
