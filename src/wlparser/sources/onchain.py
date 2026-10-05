"""Наблюдение за сетью: подтверждённые минты новых коллекций.

Соцсети дают слухи, блокчейн даёт факты — это единственный источник,
подтверждающий, что событие действительно произошло.

Два решения, продиктованных реальной сетью, а не теорией:

1. Robinhood Chain выдаёт около десяти блоков в секунду. Обходить блоки
   по одному невозможно: чтобы просто не отставать, понадобилось бы больше
   двадцати запросов в секунду, и публичный RPC отвечает на это отказом.
   Поэтому логи запрашиваются диапазонами блоков, а нулевой адрес
   отправителя фильтруется на стороне сервера — из десятков тысяч событий
   до нас доезжают только минты.

2. Алерт даёт не первый минт, а факт, что контракт собрал достаточно
   РАЗНЫХ кошельков за короткое время. Первый минт одинаково выглядит
   и у настоящей раздачи, и у теста разработчика, и у служебной NFT.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..chain.contracts import ContractInfo, ContractInspector
from ..chain.selectors import TRANSFER_SINGLE_TOPIC, TRANSFER_TOPIC, ZERO_TOPIC
from ..models import Signal, format_amount
from .filters import ContractFilter
from .mint_tracker import MintStats, MintTracker

log = logging.getLogger(__name__)

# Ограничение эндпоинта: запрос логов шире 1000 блоков отклоняется.
MAX_LOG_RANGE = 1000


class OnchainSource:
    name = "onchain"

    def __init__(
        self,
        client,
        *,
        chain_name: str,
        start_block: int,
        poll_interval: float = 2.0,
        surge_threshold: int = 25,
        confirmations: int = 2,
        min_unique_minters: int = 10,
        mint_window_minutes: int = 120,
        contract_filter: ContractFilter | None = None,
        log_range_blocks: int = 500,
        max_lag_blocks: int = 5000,
    ):
        self._client = client
        self._inspector = ContractInspector(client)
        self._chain = chain_name
        self._next_block = start_block
        self._poll_interval = poll_interval
        self._surge_threshold = surge_threshold
        self._confirmations = confirmations
        self._filter = contract_filter or ContractFilter()
        self._range = min(max(log_range_blocks, 1), MAX_LOG_RANGE)
        self._max_lag = max_lag_blocks
        self._min_unique_minters = min_unique_minters
        self._tracker = MintTracker(
            min_unique_minters=min_unique_minters,
            window_minutes=mint_window_minutes,
        )
        self._contracts: dict[str, ContractInfo] = {}
        self._ignored: set[str] = set()
        self._surged: set[str] = set()

    @property
    def next_block(self) -> int:
        """Блок, с которого продолжится обход. Используется для курсора."""
        return self._next_block

    # --- основной цикл ---

    async def run(self, out: "asyncio.Queue[Signal]") -> None:
        backoff = 1.0
        while True:
            try:
                head = int(await self._client.call("eth_blockNumber"), 16)
                safe_head = head - self._confirmations
                self._skip_ahead_if_hopeless(safe_head)

                while self._next_block <= safe_head:
                    to_block = min(self._next_block + self._range - 1, safe_head)
                    await self.process_range(self._next_block, to_block, out)
                    self._next_block = to_block + 1

                backoff = 1.0
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сбой опроса сети, повтор через %.1f с", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _skip_ahead_if_hopeless(self, safe_head: int) -> None:
        """Слишком большое отставание не догоняется — и не нужно.

        При десяти блоках в секунду сутки простоя дают почти миллион блоков.
        Разбирать их бессмысленно: те раздачи давно закончились, а пока мы
        их разбираем, мы пропускаем то, что происходит сейчас.
        """
        lag = safe_head - self._next_block
        if lag <= self._max_lag:
            return
        target = max(safe_head - self._range, 0)
        log.warning(
            "отставание %d блоков больше допустимых %d — перехожу к текущему "
            "моменту, с блока %d (пропущено %d)",
            lag,
            self._max_lag,
            target,
            target - self._next_block,
        )
        self._next_block = target

    # --- обработка диапазона блоков ---

    async def process_range(
        self, from_block: int, to_block: int, out: "asyncio.Queue[Signal]"
    ) -> None:
        minters = await self._fetch_mints(from_block, to_block)
        if not minters:
            return

        ts = datetime.now(timezone.utc)
        for address, wallets in minters.items():
            if address in self._ignored:
                continue

            info = await self._resolve(address)
            if info is None:
                continue

            stats: MintStats | None = None
            for wallet in wallets:
                result = self._tracker.record(address, wallet, ts)
                stats = stats or result

            if stats is not None:
                # Цена и лимит стоят одиннадцати вызовов, поэтому добираются
                # только для контракта, о котором мы действительно сообщим.
                info = await self._inspector.with_pricing(info)
                self._contracts[address] = info
                await out.put(self._mint_signal(info, stats, ts, to_block))
                # Всплеск в том же диапазоне — то же самое событие другими
                # словами. Пропускаем только этот раз: если проект разгонится
                # позже, всплеск об этом сообщит — это уже другая новость.
                continue

            await self._maybe_surge(info, wallets, ts, to_block, out)

    async def _fetch_mints(
        self, from_block: int, to_block: int
    ) -> dict[str, list[str | None]]:
        """Минты диапазона, сгруппированные по контракту.

        Нулевой адрес отправителя задаётся прямо в фильтре запроса, поэтому
        сеть возвращает только минты, а не все переводы токенов подряд.
        Позиция отправителя различается: в ERC-721 Transfer это topics[1],
        а в ERC-1155 TransferSingle topics[1] это operator, отправитель — topics[2].
        """
        window = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        erc721, erc1155 = await self._client.batch(
            [
                (
                    "eth_getLogs",
                    [{**window, "topics": [TRANSFER_TOPIC, ZERO_TOPIC]}],
                ),
                (
                    "eth_getLogs",
                    [{**window, "topics": [TRANSFER_SINGLE_TOPIC, None, ZERO_TOPIC]}],
                ),
            ]
        )

        grouped: dict[str, list[str | None]] = {}
        for entries, recipient_index in ((erc721 or [], 2), (erc1155 or [], 3)):
            for entry in entries:
                address = entry["address"].lower()
                grouped.setdefault(address, []).append(
                    _topic_address(entry.get("topics"), recipient_index)
                )
        return grouped

    async def _maybe_surge(self, info, wallets, ts, block, out) -> None:
        """Всплеск считается по кошелькам, а не по штукам.

        Один бот, скупивший коллекцию, — это не ажиотаж, а один бот.
        Сообщаем один раз, иначе горячий минт зальёт чат.
        """
        unique = len({wallet for wallet in wallets if wallet})
        if (
            len(wallets) < self._surge_threshold
            or unique < self._min_unique_minters
            or info.address in self._surged
        ):
            return

        self._surged.add(info.address)
        await out.put(
            Signal(
                source="onchain",
                kind="mint_surge",
                text=(
                    f"Mint surge: {info.label} — {unique} wallets, "
                    f"{len(wallets)} minted in the last blocks"
                ),
                url=None,
                author=None,
                contract_address=info.address,
                chain=self._chain,
                timestamp=ts,
                raw={"block": block, "mints": len(wallets), "wallets": unique},
            )
        )

    def _mint_signal(self, info, stats: MintStats, ts, block) -> Signal:
        minutes = max(round(stats.minutes_active), 1)
        parts = [
            f"Mint confirmed: {info.label}",
            f"{stats.unique_minters} wallets",
            f"{stats.total_mints} minted in ~{minutes} min",
        ]
        if info.mint_price_wei is not None:
            eth = info.mint_price_wei / 1e18
            parts.append("free mint" if eth == 0 else f"price {format_amount(eth)}")
        if info.max_supply:
            parts.append(f"max supply {info.max_supply}")

        return Signal(
            source="onchain",
            kind="mint_started",
            text=", ".join(parts),
            url=None,
            author=None,
            contract_address=info.address,
            chain=self._chain,
            timestamp=ts,
            raw={
                "info": dict(info.__dict__),
                "block": block,
                "unique_minters": stats.unique_minters,
                "total_mints": stats.total_mints,
                "minutes_active": round(stats.minutes_active, 1),
            },
        )

    # --- вспомогательное ---

    async def _resolve(self, address: str) -> ContractInfo | None:
        """Метаданные контракта, если он NFT-коллекция и не в чёрном списке.

        Результат кешируется в обе стороны: и подходящие контракты, и
        отброшенные, чтобы не опрашивать сеть на каждом диапазоне.
        """
        address = address.lower()
        if address in self._ignored:
            return None

        info = self._contracts.get(address)
        if info is not None:
            return info

        info = await self._inspector.inspect(address)
        if not info.is_nft or self._filter.is_ignored(info):
            self._ignored.add(address)
            log.debug("контракт отброшен фильтром: %s (%s)", address, info.name)
            return None

        self._contracts[address] = info
        log.info("новая коллекция в поле зрения: %s (%s)", info.label, address)
        return info


def _topic_address(topics, index: int) -> str | None:
    if not topics or len(topics) <= index:
        return None
    return "0x" + topics[index][-40:]
