"""Разбор текста соцсетей: адреса, ссылки, признаки WL.

Чистые функции без сети — именно здесь решается, что считать анонсом
раздачи, а что трёпом. Логика общая для Twitter и Discord.
"""

from __future__ import annotations

import re

# Адрес контракта: ровно 40 hex-символов, не часть более длинной строки
# (иначе хеш транзакции из 64 символов даст ложное совпадение).
ADDRESS_RE = re.compile(r"(?<![0-9a-fA-Fx])(0x[0-9a-fA-F]{40})(?![0-9a-fA-F])")

OPENSEA_RE = re.compile(
    r"opensea\.io/(?:collection|assets/\w+)/([A-Za-z0-9][A-Za-z0-9_-]{1,80})",
    re.IGNORECASE,
)

MAGICEDEN_RE = re.compile(
    r"magiceden\.(?:io|us)/(?:collections?/)?(?:\w+/)?([A-Za-z0-9][A-Za-z0-9_-]{1,80})",
    re.IGNORECASE,
)

# Фразы, после которых раздача уже идёт или вот-вот начнётся.
STRONG_PHRASES = (
    "mint is live",
    "minting is live",
    "mint live",
    "minting now",
    "mint now",
    "public mint",
    "free mint",
    "wl mint",
    "whitelist mint",
    "allowlist open",
    "whitelist open",
    "wl open",
    "wl raffle",
    "whitelist raffle",
    "wl spots",
    "whitelist spots",
    "allowlist spots",
    "wl giveaway",
    "wl drop",
    "claim your wl",
    "guaranteed wl",
    "og role",
)

# Отдельные слова — слабее фраз, нужны в паре с чем-то ещё.
WEAK_WORDS = ("whitelist", "allowlist", "raffle", "mint", "drop", "airdrop")

# Просьбы накрутить активность. Почти всегда пустышка.
SPAM_PATTERNS = (
    re.compile(r"\bfollow\b.{0,30}\b(rt|retweet)\b", re.IGNORECASE),
    re.compile(r"\b(rt|retweet)\b.{0,30}\bfollow\b", re.IGNORECASE),
    re.compile(r"tag\s+\d+\s+(friends?|frens?|people)", re.IGNORECASE),
    re.compile(r"like\s*\+\s*(rt|retweet|follow)", re.IGNORECASE),
    re.compile(r"drop\s+(?:your\s+)?(?:eth\s+)?(?:wallet|address)", re.IGNORECASE),
)

WL_TOKEN_RE = re.compile(r"\bwl\b", re.IGNORECASE)

# Трейдинг-боты по мемкоинам (не NFT) пишут про "mint authority" контракта
# токена — это право эмиссии, а не старт NFT-раздачи, — из-за чего адрес
# контракта + слово "mint" в тексте раньше засчитывались как wl_announced.
# Найдено на реальных данных (~7% всех wl_announced): у поста @memmememjk
# и похожих ботов свой узнаваемый язык анализа мемкоина (MC/Liq в
# долларах, holders, honeypot/renounced, "smart wallet"), которого не
# бывает в анонсе NFT-раздачи, — и по нему их отличить надёжнее, чем
# пытаться угадывать смысл слова "mint" по контексту.
MEMECOIN_SIGNAL_MARKERS = (
    "ai signal",
    "smart wallet",
    "smart money",
    "bonding curve",
    "honeypot",
    "not financial advice",
    "dyor",
)


def is_memecoin_signal_spam(text: str) -> bool:
    """Два и больше признака сразу — трейдинг-бот, а не иногда проскакивающее

    случайное слово из его словаря в обычном твите.
    """
    lowered = (text or "").lower()
    hits = sum(1 for marker in MEMECOIN_SIGNAL_MARKERS if marker in lowered)
    return hits >= 2


def extract_addresses(text: str) -> list[str]:
    """Адреса контрактов в нижнем регистре, без повторов, в порядке появления."""
    seen: dict[str, None] = {}
    for match in ADDRESS_RE.findall(text or ""):
        seen.setdefault(match.lower(), None)
    return list(seen)


def extract_marketplace_slugs(text: str) -> list[str]:
    """Идентификаторы коллекций из ссылок на маркетплейсы."""
    seen: dict[str, None] = {}
    for pattern in (OPENSEA_RE, MAGICEDEN_RE):
        for match in pattern.findall(text or ""):
            seen.setdefault(match.lower(), None)
    return list(seen)


def has_strong_phrase(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in STRONG_PHRASES)


def has_wl_mention(text: str) -> bool:
    """Упоминание WL как отдельного слова, а не куска другого слова."""
    lowered = (text or "").lower()
    if WL_TOKEN_RE.search(lowered):
        return True
    return any(word in lowered for word in WEAK_WORDS)


def is_engagement_bait(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SPAM_PATTERNS)


def classify(text: str) -> str:
    """Тип сигнала для сообщения из соцсети.

    `wl_announced` — похоже на реальный анонс раздачи или старта минта.
    `chatter` — тема упомянута, но без признаков того, что что-то началось.
    """
    if is_engagement_bait(text) or is_memecoin_signal_spam(text):
        return "chatter"
    if has_strong_phrase(text):
        return "wl_announced"
    # Отдельное слово «whitelist» ничего не значит, но вместе с адресом
    # контракта или ссылкой на маркетплейс — уже конкретика.
    if has_wl_mention(text) and (
        extract_addresses(text) or extract_marketplace_slugs(text)
    ):
        return "wl_announced"
    return "chatter"
