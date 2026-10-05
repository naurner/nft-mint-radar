"""Определение стандарта контракта и чтение метаданных через eth_call."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .abi import decode_bool, decode_string, decode_uint, encode_bytes4_arg
from .selectors import (
    ERC721_INTERFACE_ID,
    ERC1155_INTERFACE_ID,
    MAX_SUPPLY_SIGNATURES,
    MINT_PRICE_SIGNATURES,
    SEL_NAME,
    SEL_SUPPORTS_INTERFACE,
    SEL_SYMBOL,
    SEL_TOTAL_SUPPLY,
    function_selector,
)

_PRICE_SELECTORS = tuple(function_selector(s) for s in MINT_PRICE_SIGNATURES)
_MAX_SUPPLY_SELECTORS = tuple(function_selector(s) for s in MAX_SUPPLY_SIGNATURES)


@dataclass(frozen=True)
class ContractInfo:
    address: str
    standard: str | None
    name: str | None = None
    symbol: str | None = None
    total_supply: int | None = None
    mint_price_wei: int | None = None
    max_supply: int | None = None

    @property
    def is_nft(self) -> bool:
        return self.standard is not None

    @property
    def label(self) -> str:
        return self.name or self.symbol or self.address


class ContractInspector:
    """Опрашивает контракт батчами вместо десятков одиночных вызовов.

    Опознание намеренно отделено от цены и лимита. Минтящихся контрактов
    в сети сотни, и почти все отсеиваются фильтром; тратить на каждый
    одиннадцать дополнительных вызовов ради необязательных полей —
    верный способ упереться в ограничение частоты запросов.
    """

    def __init__(self, client):
        self._client = client

    @staticmethod
    def _call(address: str, data: str) -> tuple[str, list]:
        return ("eth_call", [{"to": address, "data": data}, "latest"])

    async def inspect(self, address: str) -> ContractInfo:
        """Дешёвое опознание: стандарт и метаданные, пять вызовов."""
        results = await self._client.batch(
            [
                self._call(
                    address,
                    SEL_SUPPORTS_INTERFACE + encode_bytes4_arg(ERC721_INTERFACE_ID),
                ),
                self._call(
                    address,
                    SEL_SUPPORTS_INTERFACE + encode_bytes4_arg(ERC1155_INTERFACE_ID),
                ),
                self._call(address, SEL_NAME),
                self._call(address, SEL_SYMBOL),
                self._call(address, SEL_TOTAL_SUPPLY),
            ]
        )
        is_721, is_1155, name, symbol, supply = results

        standard = None
        if decode_bool(is_721):
            standard = "erc721"
        elif decode_bool(is_1155):
            standard = "erc1155"

        return ContractInfo(
            address=address.lower(),
            standard=standard,
            name=decode_string(name),
            symbol=decode_string(symbol),
            total_supply=decode_uint(supply),
        )

    async def with_pricing(self, info: ContractInfo) -> ContractInfo:
        """Добирает цену и лимит минта — только для того, о чём сообщаем.

        Ни то, ни другое не стандартизировано, поэтому перебираются
        распространённые сигнатуры и берётся первый ответивший вариант.
        """
        if info.mint_price_wei is not None and info.max_supply is not None:
            return info

        results = await self._client.batch(
            [
                self._call(info.address, sel)
                for sel in (*_PRICE_SELECTORS, *_MAX_SUPPLY_SELECTORS)
            ]
        )
        prices = results[: len(_PRICE_SELECTORS)]
        supplies = results[len(_PRICE_SELECTORS) :]

        return replace(
            info,
            mint_price_wei=_first_not_none(decode_uint(p) for p in prices),
            max_supply=_first_not_none(decode_uint(s) for s in supplies),
        )


def _first_not_none(values: Iterable[int | None]) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None
