"""Последняя проверка сигнала перед отправкой — решает локальная модель.

Работает поверх, а не вместо структурных фильтров (sources/filters.py,
pipeline/noise.py): те про повторы, частоту и известные шаблоны, а это —
про то, выглядит ли сам текст спамом или откровенно мусорной коллекцией,
что фиксированными правилами не поймать. Место в конвейере — Pipeline._handle,
сразу после NoiseFilter и перед самой отправкой: сигнал к этому моменту уже
записан в базу независимо от вердикта, придержать можно только уведомление.

Всегда пропускает при любой неопределённости: модель недоступна, ответ
пустой или неоднозначный, случился таймаут — сигнал идёт дальше как есть.
Пропустить шум — досадно, погасить настоящую находку — гораздо хуже, и
обе ошибки не равноценны по цене, поэтому сомнение трактуется в пользу
показа, а не наоборот.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Не про качество самой модели, а про то, чтобы одна медленная проверка не
# задержала всю очередь алертов — тот же урок, что и с on_delivered раньше
# в этом проекте (см. pipeline/runner.py). Изначально стояло 8 с, но на
# живом Маке (MacBook Air, инференс на CPU, без выделенной GPU) даже
# qwen2.5:3b на короткий ответ регулярно не укладывается — фильтр почти
# всегда падал по таймауту и просто ничего не отсеивал. 20 с — с запасом,
# и по‑прежнему разы быстрее, чем ждать её 90-секундный основной таймаут
# (см. LLMSettings.timeout_s) на КАЖДЫЙ сигнал.
DEFAULT_TIMEOUT_S = 20.0


def quality_prompt(kind: str, text: str) -> tuple[str, str]:
    """(системная инструкция, сообщение) для локальной модели."""
    system = (
        "You screen alerts for a bot that watches NFT mints and whitelist "
        "announcements on Robinhood Chain, before a human sees them. Decide "
        "if the message below is worth showing, using only its text. Answer "
        "NO only for obvious spam or engagement-farming templates, generic "
        "hype with no real information, or a collection name that is clearly "
        "a test, placeholder, or junk. If in doubt, answer YES — missing a "
        "real find is worse than one extra alert. "
        "Reply with exactly one word: YES or NO."
    )
    return system, f"[{kind}] {text}"


class SignalQualityFilter:
    def __init__(self, llm, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._llm = llm
        self._timeout = timeout_s

    async def allow(self, signal) -> bool:
        """False — только явный, быстрый и уверенный "NO" от модели."""
        if self._llm is None:
            return True

        system, user = quality_prompt(signal.kind, signal.text)
        try:
            raw = await asyncio.wait_for(
                self._llm.chat(system, user, max_tokens=5, temperature=0),
                timeout=self._timeout,
            )
        except Exception:
            log.warning(
                "фильтр качества недоступен, пропускаю сигнал как есть", exc_info=True
            )
            return True

        if not raw:
            return True
        return not raw.strip().upper().startswith("NO")
