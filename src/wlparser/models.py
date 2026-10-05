"""Нормализованные модели предметной области. Без I/O."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SourceName = Literal["onchain", "twitter", "discord"]
SignalKind = Literal[
    "contract_deployed",
    "mint_started",
    "mint_surge",
    "mint_near_sellout",
    "wl_announced",
    "chatter",
]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Реальные NFT-раздачи считают саплай тысячами, редко сотнями тысяч.
# Саплай за пределами этого — верный признак того, что supportsInterface
# дал ложное "да" на контракте, который на самом деле не NFT (например,
# ERC-20 со своим collision-совпадением по селектору): у такого токена
# totalSupply() легко возвращает 10^18 и больше при 18 знаках после
# запятой, что на порядки превышает любую настоящую коллекцию.
_MAX_PLAUSIBLE_NFT_SUPPLY = 10**15


def looks_like_junk_contract(
    name: str | None, symbol: str | None, total_supply: int | None
) -> bool:
    """Контракт без имени и символа, либо с абсурдным саплаем — не раздача.

    Общая для отсева при обнаружении (`sources/filters.py`) и для остановки
    уже отслеживаемого проекта, если он оказался таким же (`tracking/service.py`) —
    контракт может пройти первую проверку, но выдать мусор на следующей.
    """
    if not name and not symbol:
        return True
    if total_supply is not None and total_supply > _MAX_PLAUSIBLE_NFT_SUPPLY:
        return True
    return False


def format_amount(value: float) -> str:
    """Число без научной записи: `4.1e-05` не читается на лету, `0.000041`
    читается. На молодых сетях типичный флор или цена — доли цента, так что
    это частый случай, не редкий, и раскладка в `e-05` не годится.

    Общая для он-чейн сигналов, живой карточки и эвристики интереса —
    все три показывают суммы в валюте сети и не должны делать это по-разному.
    """
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class Signal:
    """Одно событие из любого источника, приведённое к общему виду."""

    source: SourceName
    kind: SignalKind
    text: str
    url: str | None
    author: str | None
    contract_address: str | None
    chain: str | None
    timestamp: datetime
    raw: dict[str, Any] = field(default_factory=dict)
    # Источники с собственным идентификатором события (id твита, id сообщения)
    # задают ключ явно: иначе два разных твита про один контракт схлопнулись бы
    # в один, и вся картина обсуждения проекта потерялась бы.
    dedupe_override: str | None = None

    def __post_init__(self) -> None:
        if self.contract_address:
            object.__setattr__(self, "contract_address", self.contract_address.lower())

    @property
    def dedupe_key(self) -> str:
        """Явный ключ, иначе адрес контракта, иначе хеш нормализованного текста.

        Нормализация схлопывает регистр и пунктуацию, поэтому перепост
        того же объявления не порождает второй алерт.
        """
        if self.dedupe_override:
            return self.dedupe_override
        if self.contract_address:
            return f"{self.source}:{self.kind}:{self.contract_address}"
        normalized = _NON_ALNUM.sub(" ", self.text.lower()).strip()
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        return f"{self.source}:{self.kind}:{digest}"
