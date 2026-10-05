"""Общий контракт всех источников сигналов."""

from __future__ import annotations

import asyncio
from typing import Protocol

from ..models import Signal


class Source(Protocol):
    name: str

    async def run(self, out: "asyncio.Queue[Signal]") -> None:
        """Работает вечно, кладёт сигналы в очередь, сам переживает свои ошибки.

        Падение одного источника не должно останавливать остальные, поэтому
        обработка исключений — обязанность самого источника, а не вызывающего.
        """
        ...
