"""Отсев контрактов, которые технически являются NFT, но раздачами не являются.

Позиция ликвидности Uniswap, домен, бейдж стейкинга — всё это ERC-721,
и всё это минтится постоянно. Без такого фильтра лента забивается
инфраструктурой, а настоящая раздача в ней теряется.
"""

from __future__ import annotations

from ..models import looks_like_junk_contract

# Слова в названии коллекции, по которым узнаётся служебная NFT.
# Список намеренно про названия, а не только про адреса: одни и те же
# протоколы разворачиваются в каждой новой сети с новыми адресами.
DEFAULT_NAME_PATTERNS = (
    "uniswap",
    "position",
    "liquidity",
    # Тикеры позиций пишут сокращённо: UNI-V3-POS, UNI-V4-POS.
    "uni-v",
    "-pos",
    " lp",
    "lp token",
    "aave",
    "compound",
    "curve",
    "sushiswap",
    "pancake",
    "balancer",
    "staked",
    "staking",
    "vault",
    "wrapped",
    "domain",
    ".eth",
    "ens ",
    "name service",
    "voucher",
    "receipt",
    "test",
)


class ContractFilter:
    def __init__(
        self,
        ignore_addresses: tuple[str, ...] | list[str] = (),
        ignore_name_patterns: tuple[str, ...] | list[str] | None = None,
    ):
        self._addresses = {a.lower() for a in ignore_addresses}
        patterns = (
            DEFAULT_NAME_PATTERNS
            if ignore_name_patterns is None
            else tuple(ignore_name_patterns)
        )
        self._patterns = tuple(p.lower() for p in patterns)

    def is_ignored(self, info) -> bool:
        if info.address.lower() in self._addresses:
            return True

        if looks_like_junk_contract(info.name, info.symbol, info.total_supply):
            # Без имени и символа — либо служебный контракт, либо недоделка.
            # Абсурдный саплай (см. models.py) — обычно ложное "да" на
            # supportsInterface у контракта, который на самом деле не NFT.
            return True

        haystack = " ".join(filter(None, [info.name, info.symbol])).lower()
        return any(pattern in haystack for pattern in self._patterns)
