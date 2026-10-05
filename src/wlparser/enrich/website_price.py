"""Цена минта с сайта проекта — резервный источник для контрактов, у которых

не нашлось ни одного из угадываемых геттеров цены (см.
`chain/selectors.py::MINT_PRICE_SIGNATURES`) — это довольно частый случай
для free-mint/allowlist-контрактов без отдельной переменной цены.

Разметка сайтов никак не стандартизирована, поэтому это грубая эвристика по
ключевым словам в тексте страницы, а не надёжный парсинг. Важное
ограничение: многие современные сайты минта рисуют цену через JavaScript
(React/Next.js), и в исходном HTML её просто не будет — тогда эвристика
ничего не находит, и это ожидаемо, а не баг. Ищем только ETH: это
единственная валюта, в которой вообще имеет смысл цена минта на Robinhood
Chain (тем же ETH платится и газ), так что чужой валюте (MATIC, SOL и т.п.
с шаблонов для других сетей) просто нет причины тут появляться.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

# (status, html | None)
Fetch = Callable[[str], Awaitable[tuple[int, Any]]]

_MAX_HTML_CHARS = 200_000  # не тратить время на регексы по гигантской странице

_KEYWORD = r"(?:mint\s*price|price|cost)"
_NUM = r"(\d+(?:\.\d+)?)"
_ETH = r"(?:WETH|ETH|Ξ)"

_FREE_RE = re.compile(rf"{_KEYWORD}\s*[:\-]?\s*free\b", re.IGNORECASE)
_NUM_THEN_ETH_RE = re.compile(rf"{_KEYWORD}\s*[:\-]?\s*{_NUM}\s*{_ETH}\b", re.IGNORECASE)
_ETH_THEN_NUM_RE = re.compile(rf"{_KEYWORD}\s*[:\-]?\s*{_ETH}\s*{_NUM}\b", re.IGNORECASE)

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_mint_price_eth(html: str | None) -> float | None:
    """Цена в ETH, найденная рядом со словом "price"/"cost", иначе None.

    0.0 — явное "free mint" / "price: free" в тексте. None означает
    "не нашли", а не "точно бесплатно": вызывающий код не должен путать
    эти два случая.
    """
    if not html:
        return None
    text = _visible_text(html[:_MAX_HTML_CHARS])

    if _FREE_RE.search(text):
        return 0.0

    match = _NUM_THEN_ETH_RE.search(text) or _ETH_THEN_NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (ValueError, IndexError):
        return None


def _visible_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text)


class WebsitePriceLookup:
    def __init__(self, fetch: Fetch):
        self._fetch = fetch

    async def mint_price_eth(self, url: str) -> float | None:
        """None и при "цена не найдена", и при недоступном сайте — не только

        по духу docstring'а модуля выше, но и на практике: раньше сетевая
        ошибка (DNS, таймаут, обрыв) улетала наверх и валила ВЕСЬ апдейт
        карточки проекта — прогресс минта, флор, всё остальное, — из-за
        одного нестабильного стороннего сайта, а не только пропускала цену.
        """
        try:
            status, html = await self._fetch(url)
        except Exception:
            return None
        if status != 200:
            return None
        return extract_mint_price_eth(html)
