"""Ссылки на проект, собранные из внешних источников."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,20}$")


def safe_url(value: str | None) -> str | None:
    """Пропускает только http(s).

    Поле project_url заполняет владелец коллекции, то есть посторонний
    человек. Схему вроде javascript: в сообщение пускать нельзя.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    return candidate


def twitter_url(username: str | None) -> str | None:
    """Собирает ссылку из хендла, отбрасывая мусор вроде пустых строк и URL."""
    if not username:
        return None
    handle = username.strip().lstrip("@")
    if not _HANDLE.match(handle):
        return None
    return f"https://x.com/{handle}"


def marketplace_url(chain: str, address: str) -> str | None:
    """Страница коллекции на OpenSea по адресу контракта, без обращения к API.

    OpenSea переадресует такой адрес на страницу коллекции. Нужно это
    потому, что публичный доступ к их API закрыли: без ключа slug узнать
    нельзя, а ссылка на маркетплейс в уведомлении нужна всегда.
    """
    if not chain or not address:
        return None
    return f"https://opensea.io/assets/{chain}/{address.lower()}"


@dataclass(frozen=True)
class ProjectLinks:
    explorer: str | None = None
    opensea: str | None = None
    website: str | None = None
    twitter: str | None = None
    discord: str | None = None
    telegram: str | None = None
    collection_name: str | None = None
    description: str | None = None
    total_supply: int | None = None
    # Slug нужен отдельно от готовой ссылки: слежение периодически опрашивает
    # /collections/{slug}/stats, и делать это через парсинг URL было бы хрупко.
    collection_slug: str | None = None

    @property
    def has_socials(self) -> bool:
        return any([self.twitter, self.discord, self.telegram, self.website])

    def merged_with(self, other: "ProjectLinks") -> "ProjectLinks":
        """Дополняет пустые поля значениями из другого набора."""
        return ProjectLinks(
            **{
                field: getattr(self, field) or getattr(other, field)
                for field in self.__dataclass_fields__
            }
        )
