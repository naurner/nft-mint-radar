"""Периодический дайджест находок — замена мгновенным алертам для всего,
кроме срочного (см. pipeline/runner.py::URGENT_KINDS: только всплеск и
почти распродано будят подписчиков сразу). Формируется из того, что уже
накопилось в базе с прошлого дайджеста: он-чейн проекты по interest_score
отдельно от твиттерных анонсов WL — они не сопоставимы по своей природе,
факт от слуха.

Время отправки — локальное время контейнера (переменная TZ, см.
docker-compose.yml), не UTC: цель — чтобы дайджест приходил по часам
владельца, а не сервера.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, time, timedelta

from ..enrich.models import marketplace_url, safe_url

log = logging.getLogger(__name__)

# Короткая оценка от локальной модели — не публичный твит, поэтому нет
# смысла держать её так же долго, как в poster/service.py: тут это
# необязательное украшение дайджеста, а не единственный путь увидеть
# находку, так что лучше отправить дайджест без пары оценок, чем
# задержать его на минуты.
TAKE_TIMEOUT_S = 20.0

# Раз в два часа, круглые сутки — владелец попросил почаще, чем три раза
# в день, но всё равно не на каждую находку.
DEFAULT_DIGEST_TIMES = tuple(time(h, 0) for h in range(0, 24, 2))

# Первый дайджест после старта бота (или после долгого простоя) не должен
# зачерпывать всю историю разом — этим окном ограничивается разумный
# максимум за один раз, даже если "с прошлого дайджеста" технически было
# гораздо дольше. Чуть больше, чем шаг между дайджестами (2 часа), чтобы
# не терять находки на стыке, если планировщик проснулся с опозданием.
MAX_LOOKBACK_HOURS = 3.0


def parse_times(values: list[str]) -> tuple[time, ...]:
    """"HH:MM" из конфига в time — невалидные строки просто пропускаются,

    а не роняют весь запуск бота из-за опечатки в config.yaml.
    """
    parsed = []
    for value in values:
        try:
            hours, minutes = value.split(":")
            parsed.append(time(int(hours), int(minutes)))
        except (ValueError, TypeError):
            log.warning("не понял время дайджеста в конфиге: %r", value)
    return tuple(parsed) or DEFAULT_DIGEST_TIMES


def next_digest_at(now: datetime, times: tuple[time, ...] = DEFAULT_DIGEST_TIMES) -> datetime:
    """Ближайший момент из `times` строго после `now`, в том же часовом поясе.

    Чистая функция от `now` — планировщик сам решает, что считать текущим
    временем (обычно `datetime.now()`, локальное время контейнера).
    """
    candidates = [
        now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0) for t in times
    ]
    upcoming = [c for c in candidates if c > now]
    if upcoming:
        return min(upcoming)
    return min(candidates) + timedelta(days=1)


def digest_since(last_sent: str | None, *, now: datetime) -> datetime:
    """Момент, с которого собирать дайджест — не раньше MAX_LOOKBACK_HOURS назад."""
    floor = now - timedelta(hours=MAX_LOOKBACK_HOURS)
    if not last_sent:
        return floor
    try:
        parsed = datetime.fromisoformat(last_sent)
    except ValueError:
        return floor
    return max(parsed, floor)


def _take_prompt(text: str) -> tuple[str, str]:
    system = (
        "You screen social posts about NFT mints and whitelist announcements "
        "on Robinhood Chain for someone deciding whether to look closer. In "
        "one short phrase (under 15 words), say what stands out about this "
        "post — genuine and specific, or generic/templated/low on real "
        "information. Base it only on the text given. "
        "Output ONLY the phrase, nothing else."
    )
    return system, f"Post: {text}"


def _clean_take(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = " ".join(raw.split()).strip("\"'`“”«» ")
    return cleaned[:140] or None


async def attach_takes(
    announcements: list[dict], llm, *, timeout_s: float = TAKE_TIMEOUT_S
) -> list[dict]:
    """Короткая оценка модели на каждый анонс — почему обратить внимание

    (или нет). Необязательно: без модели, при её неудаче или слишком
    долгом ответе анонс просто показывается без оценки, как раньше —
    это украшение дайджеста, а не единственный способ его увидеть.
    """
    if llm is None:
        return announcements

    enriched = []
    for row in announcements:
        row = dict(row)
        try:
            raw = await asyncio.wait_for(
                llm.chat(*_take_prompt(row.get("text") or ""), max_tokens=40, temperature=0.4),
                timeout=timeout_s,
            )
        except Exception:
            log.warning("не удалось получить оценку анонса для дайджеста", exc_info=True)
            raw = None
        take = _clean_take(raw)
        if take:
            row["take"] = take
        enriched.append(row)
    return enriched


def format_digest(projects: list[dict], announcements: list[dict]) -> str | None:
    """None — собирать было не из чего, в этот раз дайджест не шлём."""
    if not projects and not announcements:
        return None

    lines = ["<b>📰 Дайджест находок</b>"]

    if projects:
        lines.append("")
        lines.append("<b>Проекты</b>")
        for row in projects:
            label = html.escape(row.get("name") or row.get("symbol") or row["address"])
            score = row.get("interest_score")
            reason = html.escape(row.get("interest_reason") or "")
            head = f"<b>{score}</b> — {label}" if score is not None else label
            lines.append(f"• {head}")
            if reason:
                lines.append(f"   {reason}")
            link = marketplace_url(row.get("chain"), row.get("address"))
            if link:
                lines.append(f'   <a href="{html.escape(link)}">OpenSea</a>')

    if announcements:
        lines.append("")
        lines.append("<b>Анонсы в твиттере</b>")
        for row in announcements:
            text = html.escape((row.get("text") or "")[:90])
            line = f"🎟 {text}"
            link = safe_url(row.get("url"))
            if link:
                line += f' — <a href="{html.escape(link)}">ссылка</a>'
            lines.append(line)
            take = row.get("take")
            if take:
                lines.append(f"   <i>{html.escape(take)}</i>")

    return "\n".join(lines)
