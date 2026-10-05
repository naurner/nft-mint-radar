"""Эвристическая оценка интереса к проекту.

Это не модель. Модели нужны недели накопленных снимков, которых пока
нет, — снимки только начинают копиться в таблице `snapshots`. Это первая
осмысленная оценка на том, что уже можно измерить сейчас: как быстро
коллекция набирает держателей и как флор соотносится с ценой минта.

Когда данных накопится на несколько недель по десяткам проектов, эти же
снимки станут обучающей выборкой, а веса ниже — первым бейзлайном,
с которым можно будет сравнить обученную модель.
"""

from __future__ import annotations

from datetime import datetime

from ..models import format_amount


def compute_interest_score(project: dict, history: list[dict]) -> tuple[int, str]:
    """Возвращает (0-100, короткое человеческое объяснение).

    `history` — снимки одного адреса по возрастанию времени (`db.snapshot_history`).
    """
    if not history:
        return 0, "недостаточно данных"

    latest = history[-1]
    score = 0
    reasons: list[str] = []

    max_supply = latest.get("max_supply") or project.get("max_supply")
    total_supply = latest.get("total_supply")
    if max_supply and total_supply is not None:
        fill = total_supply / max_supply
        if fill >= 0.95:
            score += 30
            reasons.append("почти распродано")
        elif fill >= 0.5:
            score += 15
            reasons.append("больше половины разобрано")

    mint_price = latest.get("mint_price_wei")
    floor = latest.get("floor_price_eth")
    # Цена минта — всегда в нативной валюте сети (ETH на EVM), а флор на
    # OpenSea бывает в чём угодно, вплоть до стейблкоина вроде USDG. Делить
    # одно на другое без учёта этого значило бы сравнивать несравнимое —
    # пропускаем этот множитель, если валюта флора не подтверждена как ETH.
    floor_is_eth = latest.get("floor_price_symbol") in ("ETH", "WETH")
    if mint_price is not None and floor is not None and floor_is_eth:
        mint_eth = mint_price / 1e18
        if mint_eth == 0:
            if floor > 0:
                score += 25
                reasons.append(f"бесплатный минт, флор {format_amount(floor)} ETH")
        else:
            ratio = floor / mint_eth
            if ratio >= 1.5:
                score += 30
                reasons.append(f"флор в {ratio:.1f}x выше цены минта")
            elif ratio >= 1.0:
                score += 15
                reasons.append("флор держится на уровне цены минта")
            else:
                score -= 15
                reasons.append(f"флор ниже цены минта ({ratio:.1f}x)")

    if len(history) >= 2:
        growth = _minters_per_hour(history[0], history[-1])
        if growth is not None:
            if growth >= 20:
                score += 20
                reasons.append(f"~{growth:.0f} новых держателей в час")
            elif growth >= 5:
                score += 10
                reasons.append(f"~{growth:.0f} новых держателей в час")

    score = max(0, min(100, score))
    reason = ", ".join(reasons) if reasons else "мало сигналов пока"
    return score, reason


def _minters_per_hour(first: dict, last: dict) -> float | None:
    try:
        t0 = _parse(first["ts"])
        t1 = _parse(last["ts"])
    except (KeyError, ValueError):
        return None

    hours = max((t1 - t0).total_seconds() / 3600, 0.1)
    a = first.get("unique_minters")
    b = last.get("unique_minters")
    if a is None or b is None:
        return None
    return (b - a) / hours


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
