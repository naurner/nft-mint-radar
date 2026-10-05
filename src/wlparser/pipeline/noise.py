"""Отсев шума перед отправкой в Telegram.

Разбор реального потока показал три источника лишних сообщений, и ни один
из них не отсекается порогом по числу подписчиков:

1. **Шаблонные посты за активность.** Проект раздаёт готовый текст, и его
   публикуют десятки человек. Тексты совпадают дословно, а аудитория
   авторов гуляет от сотни до девяти тысяч — по размеру аудитории такое
   не различить, только по совпадению текста.

2. **Один проект во множестве сообщений.** Половина потока была про один
   HoodMiners: разные люди, разные слова, новости никакой.

3. **Просто много всего сразу.** Даже полезные находки, приходящие пачкой
   по тридцать штук, перестают читаться.

Всё отклонённое сохраняется в базу и доступно через /recent — фильтр
решает, о чём будить, а не что запоминать.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@(\w{1,20})")
_NON_WORD = re.compile(r"[^a-zа-яё0-9]+")


def normalize(text: str) -> set[str]:
    """Слова текста без ссылок, упоминаний и знаков.

    Ссылки и упоминания убираются намеренно: в шаблонных постах меняют
    именно их, оставляя тело текста нетронутым.
    """
    stripped = _MENTION.sub(" ", _URL.sub(" ", (text or "").lower()))
    return {word for word in _NON_WORD.split(stripped) if len(word) > 2}


def similarity(a: set[str], b: set[str]) -> float:
    """Доля общих слов. Единица — тексты состоят из одних и тех же слов."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def topic_key(signal) -> str | None:
    """Про что сообщение: адрес контракта, иначе упомянутые аккаунты."""
    if signal.contract_address:
        return f"contract:{signal.contract_address}"
    mentions = sorted({m.lower() for m in _MENTION.findall(signal.text or "")})
    if mentions:
        return "mentions:" + ",".join(mentions[:3])
    return None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


class NoiseFilter:
    def __init__(
        self,
        *,
        similarity_threshold: float = 0.7,
        recent_texts: int = 300,
        text_window_hours: float = 12.0,
        topic_cooldown_minutes: float = 240.0,
        onchain_topic_cooldown_minutes: float = 30.0,
        max_alerts_per_hour: int = 8,
        min_author_followers: int = 100,
        urgent_score: int = 85,
    ):
        self._similarity = similarity_threshold
        self._text_window = timedelta(hours=text_window_hours)
        self._topic_cooldown = timedelta(minutes=topic_cooldown_minutes)
        self._onchain_cooldown = timedelta(minutes=onchain_topic_cooldown_minutes)
        self._max_per_hour = max_alerts_per_hour
        self._min_followers = min_author_followers
        self._urgent = urgent_score

        self._texts: deque[tuple[datetime, set[str]]] = deque(maxlen=recent_texts)
        self._topics: dict[str, datetime] = {}
        self._sent: deque[datetime] = deque()

    def allow(self, signal, score: int, now: datetime) -> Decision:
        decision = self._check(signal, score, now)
        if decision.allowed:
            self._remember(signal, now)
        else:
            log.debug("сигнал придержан (%s): %s", decision.reason, signal.text[:60])
        return decision

    # --- правила ---

    def _check(self, signal, score: int, now: datetime) -> Decision:
        urgent = score >= self._urgent

        if not self._within_hourly_cap(now) and not urgent:
            return Decision(False, "превышен лимит сообщений в час")

        # Он-чейн событие — факт, а не чужой текст: проверки на шаблонность
        # и на размер аудитории к нему неприменимы. Но повторяться про один
        # контракт он тоже не должен: «начался минт» и «всплеск» — это одно
        # и то же событие, пересказанное дважды. «Почти распродано» —
        # исключение: это новый этап той же раздачи, а не пересказ старого,
        # и у быстрых минтов он может наступить в считанные минуты после
        # старта — раньше, чем истечёт обычный кулдаун по контракту. Он и
        # так шлётся не больше одного раза за жизнь проекта (см. LiveTracker),
        # так что общий кулдаун ему просто не нужен.
        if signal.source == "onchain":
            key = topic_key(signal)
            if key is not None and signal.kind != "mint_near_sellout":
                last = self._topics.get(key)
                if last is not None and now - last < self._onchain_cooldown:
                    return Decision(False, "про этот контракт только что сообщали")
            return Decision(True)

        followers = (signal.raw or {}).get("author_followers")
        if followers is not None and followers < self._min_followers:
            return Decision(False, f"у автора всего {followers} подписчиков")

        if self._is_template(signal, now):
            return Decision(False, "шаблонный пост, такой текст уже был")

        key = topic_key(signal)
        if key and not urgent:
            last = self._topics.get(key)
            if last is not None and now - last < self._topic_cooldown:
                return Decision(False, "про этот проект недавно уже сообщали")

        return Decision(True)

    def _within_hourly_cap(self, now: datetime) -> bool:
        cutoff = now - timedelta(hours=1)
        while self._sent and self._sent[0] < cutoff:
            self._sent.popleft()
        return len(self._sent) < self._max_per_hour

    def _is_template(self, signal, now: datetime) -> bool:
        words = normalize(signal.text)
        if len(words) < 4:
            return False
        cutoff = now - self._text_window
        return any(
            seen_at >= cutoff and similarity(words, seen) >= self._similarity
            for seen_at, seen in self._texts
        )

    def _remember(self, signal, now: datetime) -> None:
        self._sent.append(now)
        if signal.source != "onchain":
            words = normalize(signal.text)
            if len(words) >= 4:
                self._texts.append((now, words))
        key = topic_key(signal)
        if key:
            self._topics[key] = now
