"""Асинхронный JSON-RPC клиент.

Транспорт инъектируется, чтобы тесты не трогали сеть.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Awaitable, Iterable, Protocol

import httpx

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Transport(Protocol):
    def __call__(self, payload: Any) -> Awaitable[Any]: ...


def http_transport(url: str | list[str], timeout: float = 20.0) -> Transport:
    """HTTP-транспорт поверх httpx.

    Принимает несколько адресов и раскладывает запросы по кругу: лимит
    частоты считается отдельно на каждом узле, поэтому два эндпоинта дают
    вдвое больший запас и переживают недоступность одного из них.
    """
    urls = [url] if isinstance(url, str) else [u for u in url if u]
    if not urls:
        raise ValueError("не задан ни один RPC-адрес")
    client = httpx.AsyncClient(timeout=timeout)
    turn = itertools.cycle(range(len(urls)))

    async def send(payload: Any) -> Any:
        index = next(turn)
        last: Exception | None = None
        # Пробуем все узлы: упавший или залимитированный не должен
        # ронять запрос, если есть живой сосед.
        for offset in range(len(urls)):
            target = urls[(index + offset) % len(urls)]
            try:
                response = await client.post(target, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                last = exc
        raise last  # type: ignore[misc]

    send.aclose = client.aclose  # type: ignore[attr-defined]
    return send


def _retry_after_seconds(exc: Exception) -> float | None:
    """Сколько просит подождать сервер при 429, если он это сообщил."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    if getattr(response, "status_code", None) != 429:
        return None
    value = None
    try:
        value = response.headers.get("retry-after")
    except Exception:
        return 5.0
    if not value:
        return 5.0
    try:
        return max(float(value), 0.0)
    except ValueError:
        return 5.0


class JsonRpcClient:
    def __init__(
        self,
        transport: Transport,
        *,
        max_retries: int = 4,
        min_request_interval_s: float = 0.0,
        sleep=asyncio.sleep,
    ):
        self._transport = transport
        self._ids = itertools.count(1)
        self._max_retries = max_retries
        # Публичные эндпоинты режут по числу запросов в секунду, поэтому
        # держим паузу между обращениями, а не ловим 429 постфактум.
        self._min_interval = min_request_interval_s
        self._sleep = sleep
        self._last_sent = 0.0
        self._gate = asyncio.Lock()

    async def aclose(self) -> None:
        closer = getattr(self._transport, "aclose", None)
        if closer is not None:
            await closer()

    async def _throttle(self) -> None:
        if not self._min_interval:
            return
        async with self._gate:
            now = time.monotonic()
            wait = self._last_sent + self._min_interval - now
            if wait > 0:
                await self._sleep(wait)
            self._last_sent = time.monotonic()

    async def _send(self, payload: Any) -> Any:
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self._max_retries):
            await self._throttle()
            try:
                return await self._transport(payload)
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last = exc
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    log.warning(
                        "RPC ограничил частоту, пауза %.1f с (попытка %d)",
                        retry_after,
                        attempt + 1,
                    )
                    if attempt + 1 < self._max_retries:
                        await self._sleep(retry_after)
                    continue
                log.warning("RPC transport error (attempt %d): %s", attempt + 1, exc)
                if attempt + 1 < self._max_retries:
                    await self._sleep(delay)
                    delay *= 2
        raise RpcError(f"transport failed after {self._max_retries} attempts: {last}")

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or [],
        }
        response = await self._send(payload)
        if "error" in response:
            err = response["error"]
            raise RpcError(err.get("message", "unknown"), err.get("code"))
        return response.get("result")

    async def batch(self, calls: Iterable[tuple[str, list[Any]]]) -> list[Any]:
        """Результаты в порядке запросов; None вместо упавших.

        Половина вызовов при опросе контракта штатно ревертится (функции
        может просто не быть), поэтому ошибка одного элемента батча —
        это нормальный результат, а не сбой всего запроса.
        """
        items = list(calls)
        if not items:
            return []

        payload = []
        order: list[int] = []
        for method, params in items:
            request_id = next(self._ids)
            order.append(request_id)
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )

        response = await self._send(payload)
        # Спека JSON-RPC не гарантирует порядок ответов в батче.
        by_id = {entry["id"]: entry for entry in response}
        results: list[Any] = []
        for request_id in order:
            entry = by_id.get(request_id)
            if entry is None or "error" in entry:
                results.append(None)
            else:
                results.append(entry.get("result"))
        return results
