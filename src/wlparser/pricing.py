"""Перевод цены минта в доллары, с учётом комиссии сети.

Курс ETH/USD берётся с CoinGecko без ключа и кешируется на несколько минут:
для наших целей курс не нужен точнее, чем раз в пару минут, а частые запросы
быстро упрутся в лимит бесплатного тарифа.

Комиссия сети — это оценка, не точный расчёт под конкретный контракт:
реальный расход газа у разных функций минта отличается в разы (от ~60k до
300k+ газа), и без выполнения самой транзакции точнее не узнать. Берём
текущую gasPrice сети и умножаем на условный расход — это ближе к правде,
чем совсем игнорировать газ, но остаётся приближением, и подписывается
как оценка в самих сообщениях.

Ни курс, ни оценка комиссии не должны задерживать или ронять алерт: если
источник недоступен, конвертация просто не показывается.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
)

# (status, json | None)
Fetch = Callable[[str, dict[str, str]], Awaitable[tuple[int, Any]]]


def format_usd_estimate(
    mint_price_wei: int, eth_rate: float, gas_fee_wei: int | None
) -> str:
    """Строка вида "$34.50 с газом" или просто "$2.15".

    Не говорит "минт бесплатный" сама — это уже сообщает вызывающий код,
    который и так пишет "бесплатно" рядом; повторять было бы избыточно.
    При бесплатном минте (mint_price_wei == 0) с учётом газа сумма — это
    ровно стоимость самой транзакции, что и есть реальная цена входа.
    """
    price_eth = mint_price_wei / 1e18

    if gas_fee_wei:
        total_usd = (price_eth + gas_fee_wei / 1e18) * eth_rate
        return f"${total_usd:.2f} с газом"

    return f"${price_eth * eth_rate:.2f}"


class EthUsdRate:
    """Курс ETH/USD с CoinGecko, с кешем и без падения при сбое источника."""

    def __init__(
        self,
        fetch: Fetch,
        *,
        cache_seconds: float = 180.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._fetch = fetch
        self._cache_seconds = cache_seconds
        self._now = now
        self._cached: float | None = None
        self._cached_at: datetime | None = None

    async def get(self) -> float | None:
        if self._is_fresh():
            return self._cached

        try:
            status, data = await self._fetch(
                COINGECKO_URL, {"accept": "application/json"}
            )
        except Exception:
            log.warning("не удалось получить курс ETH/USD, использую прошлый")
            return self._cached

        if status != 200 or not isinstance(data, dict):
            log.warning("CoinGecko вернул HTTP %s для курса ETH/USD", status)
            return self._cached

        try:
            rate = float(data["ethereum"]["usd"])
        except (KeyError, TypeError, ValueError):
            log.warning("неожиданный формат ответа CoinGecko: %r", data)
            return self._cached

        self._cached = rate
        self._cached_at = self._now()
        return rate

    def _is_fresh(self) -> bool:
        if self._cached is None or self._cached_at is None:
            return False
        return (self._now() - self._cached_at).total_seconds() < self._cache_seconds


class GasFeeEstimator:
    """Оценка комиссии сети: текущая gasPrice × условный расход газа на минт."""

    def __init__(
        self,
        client,
        *,
        gas_per_mint: int = 150_000,
        cache_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._client = client
        self._gas_per_mint = gas_per_mint
        self._cache_seconds = cache_seconds
        self._now = now
        self._cached: int | None = None
        self._cached_at: datetime | None = None

    async def estimate_fee_wei(self) -> int | None:
        if self._is_fresh():
            return self._cached

        try:
            raw = await self._client.call("eth_gasPrice")
            gas_price = int(raw, 16)
        except Exception:
            log.warning("не удалось получить gasPrice, использую прошлую оценку")
            return self._cached

        self._cached = gas_price * self._gas_per_mint
        self._cached_at = self._now()
        return self._cached

    def _is_fresh(self) -> bool:
        if self._cached is None or self._cached_at is None:
            return False
        return (self._now() - self._cached_at).total_seconds() < self._cache_seconds


class PriceConverter:
    """Собирает курс и комиссию в одну готовую строку для сообщений."""

    def __init__(
        self,
        rate: EthUsdRate,
        gas: GasFeeEstimator | None = None,
        *,
        include_gas: bool = True,
    ):
        self._rate = rate
        self._gas = gas
        self._include_gas = include_gas

    async def usd_line(self, mint_price_wei: int | None) -> str | None:
        if mint_price_wei is None:
            return None

        rate = await self._rate.get()
        if rate is None:
            return None

        gas_fee = None
        if self._include_gas and self._gas is not None:
            gas_fee = await self._gas.estimate_fee_wei()

        return format_usd_estimate(mint_price_wei, rate, gas_fee)

    async def usd_amount(self, amount_eth: float | None) -> str | None:
        """Курс без комиссии газа — для сумм, которые не сами транзакции

        (флор, объём, цены продаж). У них нет своего газа, который можно
        было бы оценить, в отличие от цены минта.
        """
        if amount_eth is None:
            return None

        rate = await self._rate.get()
        if rate is None:
            return None

        return f"${amount_eth * rate:.2f}"
