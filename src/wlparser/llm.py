"""Локальная языковая модель через OpenAI-совместимый API (Ollama).

Тот же способ подключения, что уже работает в соседнем проекте Lalascaner
(его README, раздел «Бесплатный ИИ»): Ollama стоит на самом Маке, не в
Docker — контейнеры на Mac не видят GPU, а нативная Ollama использует
Metal и работает быстро, — а из контейнера до неё достаёт адрес
`host.docker.internal:11434` (см. extra_hosts в docker-compose.yml).

Контракт для вызывающего кода один: `chat()` НЕ бросает исключений. Ollama
может быть не запущена, модель не скачана, ответ — мусором; всё это для
бота штатные ситуации, а не сбой, и любая фича поверх модели обязана
иметь запасной путь без неё (см. poster/service.py — шаблонный текст).
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

# Список моделей у Ollama включает и эмбеддинговые — они не умеют в чат.
_NOT_CHAT_MARKERS = ("embed",)


class LocalLLM:
    def __init__(
        self,
        *,
        base_url: str,
        model: str = "",
        api_key: str = "not-needed",
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._model = model.strip()
        self._resolved: str | None = None
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def resolve_model(self) -> str | None:
        """Закреплённая в настройках модель, иначе первая чатовая из скачанных.

        Автовыбор — чтобы не заставлять владельца гадать, как называется
        то, что он скачал (`llama3.1`, `qwen2.5:14b`, ...). Запоминается
        только удачный ответ: если модель докачают позже, следующий вызов
        её увидит.
        """
        if self._model:
            return self._model
        if self._resolved:
            return self._resolved

        response = await self._http.get("/models", timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        for entry in entries or []:
            name = entry.get("id") if isinstance(entry, dict) else None
            if name and not any(marker in name.lower() for marker in _NOT_CHAT_MARKERS):
                self._resolved = name
                return name
        return None

    async def health(self) -> tuple[bool, str]:
        """(работает ли, короткое объяснение для строки статуса в /menu)."""
        try:
            model = await self.resolve_model()
        except httpx.HTTPError as exc:
            return False, f"не отвечает ({exc.__class__.__name__})"
        except ValueError:
            return False, "ответ не похож на Ollama"
        if not model:
            return False, "запущена, но нет скачанных моделей (ollama pull llama3.1)"
        return True, f"модель {model}"

    async def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> str | None:
        """Текст ответа модели или None — при любом сбое, без исключений."""
        try:
            model = await self.resolve_model()
            if not model:
                log.warning("локальный ИИ: нет ни одной подходящей модели")
                return None

            response = await self._http.post(
                "/chat/completions",
                json={
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("локальный ИИ не ответил: %s: %s", exc.__class__.__name__, exc)
            return None

        content = (content or "").strip()
        return content or None
