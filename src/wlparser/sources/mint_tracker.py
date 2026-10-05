"""Накопление статистики минта, чтобы отличать раздачу от шума.

Первый минт контракта не значит ничего: так выглядит и тест разработчика,
и служебная NFT вроде позиции ликвидности. Раздачу отличает другое —
много РАЗНЫХ кошельков за короткое время. Трекер копит статистику и
сообщает о контракте ровно один раз, когда тот переваливает порог.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class MintStats:
    address: str
    unique_minters: int
    total_mints: int
    first_seen: datetime
    last_seen: datetime

    @property
    def minutes_active(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds() / 60


@dataclass
class _Entry:
    first_seen: datetime
    last_seen: datetime
    total_mints: int = 0
    minters: set[str] = field(default_factory=set)
    reported: bool = False


class MintTracker:
    def __init__(
        self,
        *,
        min_unique_minters: int = 10,
        window_minutes: int = 120,
        max_tracked: int = 5000,
    ):
        self._min_unique = min_unique_minters
        self._window = timedelta(minutes=window_minutes)
        self._max_tracked = max_tracked
        self._entries: dict[str, _Entry] = {}

    def record(self, address: str, minter: str | None, ts: datetime) -> MintStats | None:
        """Учитывает один минт.

        Возвращает статистику только в тот момент, когда контракт впервые
        перевалил порог уникальных минтеров — то есть ровно один раз.
        """
        address = address.lower()
        entry = self._entries.get(address)

        if entry is None:
            self._evict_if_needed(ts)
            entry = _Entry(first_seen=ts, last_seen=ts)
            self._entries[address] = entry
        elif ts - entry.first_seen > self._window:
            # Окно истекло: контракт минтится вяло и долго — это не раздача.
            # Считаем заново, не сбрасывая отметку о том, что уже отчитались.
            reported = entry.reported
            entry = _Entry(first_seen=ts, last_seen=ts, reported=reported)
            self._entries[address] = entry

        entry.last_seen = ts
        entry.total_mints += 1
        if minter:
            entry.minters.add(minter.lower())

        if entry.reported or len(entry.minters) < self._min_unique:
            return None

        entry.reported = True
        return MintStats(
            address=address,
            unique_minters=len(entry.minters),
            total_mints=entry.total_mints,
            first_seen=entry.first_seen,
            last_seen=entry.last_seen,
        )

    def is_reported(self, address: str) -> bool:
        entry = self._entries.get(address.lower())
        return bool(entry and entry.reported)

    def _evict_if_needed(self, now: datetime) -> None:
        """Не даём словарю расти бесконечно на долгом прогоне."""
        if len(self._entries) < self._max_tracked:
            return
        stale = [
            address
            for address, entry in self._entries.items()
            if not entry.reported and now - entry.last_seen > self._window
        ]
        for address in stale:
            del self._entries[address]

        if len(self._entries) >= self._max_tracked:
            oldest = sorted(self._entries.items(), key=lambda kv: kv[1].last_seen)
            for address, _ in oldest[: self._max_tracked // 4]:
                del self._entries[address]
