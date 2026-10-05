"""Текст твита про находку — собственный пост и цитирование чужого.

Собственный пост (`build_caption`/`caption_prompt`) — только он-чейн факты
(source == "onchain"). Он-чейн сигнал сам по себе уже готовый, проверенный
сетью текст (см. sources/onchain.py).

Есть два пути к одному и тому же твиту. Шаблонный (`build_caption`) — сам
текст сигнала как есть, без сети и без ИИ, работает всегда. ИИ-путь
(`caption_prompt` + `clean_generated_body` + `compose`) — локальная модель
переписывает тот же факт живее; он необязателен, и любая его неудача —
пустой ответ, ссылка, выдуманная цифра — просто возвращает к шаблону.
Ссылку и хэштеги модель не пишет никогда: хвост всегда собирается кодом.

Решение "стоит ли вообще постить" (владелец попросил отбирать по
качеству, не по счётчику) зашито в тот же запрос, что пишет текст, а не
отдельный запрос до него — см. SKIP_TOKEN/is_skip_reply. Раньше это были
два последовательных запроса к модели на каждую находку, и локальная
модель на этом железе не успевала ответить ни на один из двух — почти
всё просто уходило в тайм-аут. Один запрос "решить и написать" — тот же
порог качества вдвое дешевле для модели.

Цитирование (`quote_prompt`) — ИИ-only, без шаблонного запасного пути:
читать чужой твит и писать к нему короткий комментарий без модели нечем,
и это осознанно, владелец сам попросил добавить такую активность. Тот же
принцип "решить и написать одним запросом" и та же проверка
`clean_generated_body` — не даёт модели придумать цифру или подтвердить
чужое заявление как факт.
"""

from __future__ import annotations

import random
import re

MAX_LEN = 280

# Публикуемые виды сигналов — факты, подтверждённые сетью (см.
# models.py::SignalKind), а не пересказ чужого текста. mint_started сюда
# специально не входит — и так очень частый случай (десятки в час в
# активные периоды), потому и постов было много: всплеск/почти
# распродано реже и само по себе куда более доказанный сигнал, чем факт
# самого старта минта.
POSTABLE_KINDS = {"mint_surge", "mint_near_sellout"}

HASHTAGS = "#RobinhoodChain #NFT"

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Модель не должна ни вставлять ссылки/упоминания/хэштеги/тикеры от себя,
# ни рекламировать: это собирает или запрещает код, а не её фантазия.
_FORBIDDEN_MARKERS = ("http", "www.", "@", "#", "$")


def _normalize_number(raw: str) -> str:
    """"2,222" и "2222" — одно и то же число, просто с разделителем разрядов.

    Без этого модель, написавшая цифру ровно так, как её обычно пишут
    в живом английском тексте, проваливала бы проверку на выдумку —
    и черновик каждый раз откатывался бы к шаблону без всякой причины.
    """
    return raw.replace(",", "")


def is_postable(source: str, kind: str) -> bool:
    return source == "onchain" and kind in POSTABLE_KINDS


def _tail(opensea_url: str | None) -> str:
    return "\n\n".join(p for p in (opensea_url, HASHTAGS) if p)


def body_budget(opensea_url: str | None) -> int:
    """Сколько символов остаётся на сам текст после ссылки и хэштегов."""
    tail = _tail(opensea_url)
    return MAX_LEN - (len(tail) + 2 if tail else 0)


def compose(body: str, opensea_url: str | None) -> str:
    tail = _tail(opensea_url)
    return f"{body}\n\n{tail}" if tail else body


def build_caption(
    *, source: str, kind: str, text: str, opensea_url: str | None = None
) -> str | None:
    """Шаблонный твит, либо None — если про это постить не стоит.

    None означает либо "не тот вид сигнала" (соцсети, слухи), либо
    "даже ссылка и хэштеги не влезли бы в лимит" — оба случая для
    вызывающего кода одинаковы: черновик не создаётся.
    """
    if not is_postable(source, kind):
        return None

    budget = body_budget(opensea_url)
    if len(text) <= budget:
        return compose(text, opensea_url)

    # Урезаем сам факт, не ссылку и не хэштеги — без них твит не найти
    # по тегу и не открыть коллекцию одним касанием.
    room = budget - 1  # запас на многоточие "…"
    if room < 10:
        return None
    return compose(text[:room].rstrip() + "…", opensea_url)


# Голос каждый раз выбирается случайно из нескольких — иначе все посты
# получаются одной и той же конструкции ("заметил X, вот что думаю"),
# что и так и выглядит как бот, просто многословный. Собрано по образцу
# того, как в X обычно пишут про быстрые минты: короче, резче, иногда с
# вопросом или цифрой в начале, а не всегда один и тот же разбор.
_VOICE_STYLES = (
    "You are a person who tracks NFT mints on Robinhood Chain and shares your "
    "own quick take when something looks worth mentioning. Don't just restate "
    "the facts in different words — say what actually stands out about them "
    "and why you'd bother posting this one: the pace, the size, how it reads "
    "compared to a typical mint. Write like a short personal reaction — first "
    "person is fine, a bit of opinion is fine — not a press release or a bot "
    "report.",
    "You write NFT call-outs on Robinhood Chain in the clipped, punchy voice "
    "crypto Twitter uses for a fast-moving mint — short fragments over full "
    "sentences are fine, a quick rhetorical question is fine, one sharp "
    "observation beats a full recap. Confident, not hypey.",
    "You comment on NFT mints on Robinhood Chain with a slightly skeptical, "
    "seen-it-before eye — noticing the pace or the numbers and reacting the "
    "way someone who's watched a hundred of these would: mildly impressed, "
    "mildly wary, never hyped, never gushing.",
    "You write one-line NFT call-outs for Robinhood Chain that lead with "
    "whichever single number in the facts is the most striking, then a short "
    "reaction to it — stat first, commentary second, no windup.",
)


# Вместо отдельного запроса "стоит ли" перед запросом текста — одной такой
# проверки на каждую находку локальной модели оказалось физически не
# унести (на MacBook Air она не успевает ответить ни на один из двух
# запросов подряд, и оба просто уходят в тайм-аут). "Решить и написать"
# одним запросом — тот же порог качества, вдвое меньше нагрузки на модель.
SKIP_TOKEN = "SKIP"


def is_skip_reply(raw: str | None) -> bool:
    return bool(raw) and raw.strip().upper() == SKIP_TOKEN


def caption_prompt(fact: str, budget: int) -> tuple[str, str]:
    """(системная инструкция, сообщение) для локальной модели.

    Голос нарочно не один фиксированный — владелец просил не только
    "осознанно" (короткую оценку, а не пересказ фактов своими словами),
    но и разнообразно, чтобы посты не звучали однотипно. Явно запрещено
    при этом считать новые числа (проценты, отношения): модель отлично
    умеет подвести к ним, но столь же легко ошибается в арифметике, а
    clean_generated_body ниже всё равно отбросит любое число, которого
    не было в исходном факте, — просить не считать дешевле, чем каждый
    раз тратить генерацию на текст, который потом откатится к шаблону.

    Факт сюда попадает только после структурного порога (всплеск — не
    меньше 25 кошельков/минтов за окно, почти распродано — не меньше 75%,
    см. config.py) — то есть это по определению уже не рядовой минт.
    Раньше SKIP просили решать "заметно ли это" с нуля, без этой границы —
    маленькая локальная модель без ориентира почти всегда отвечала "нет",
    даже на 85% распродано. Теперь SKIP — только для действительно слабых
    случаев (впритык к порогу) или сломанной строки, не replay той же
    оценки, которую уже сделал код.
    """
    voice = random.choice(_VOICE_STYLES)
    system = (
        f"{voice} The numbers below already passed an automatic filter "
        "(a minimum pace of unique wallets, or a minimum percent sold) "
        "before reaching you — so by definition this is not a routine, "
        "ordinary mint, and you should normally write the post. Reply "
        f"with exactly {SKIP_TOKEN} and nothing else only if the numbers "
        "are weak even by that bar (barely past the minimum) or the line "
        "looks broken, duplicated, or templated — not merely because it "
        "isn't huge. "
        "Rules: English; at most "
        f"{budget} characters; base your take ONLY on the facts given below — "
        "never invent or guess prices, dates, names, or numbers not in them, and "
        "don't calculate new numbers either (no percentages, no ratios, nothing "
        "derived) — react to the numbers given, don't do math on them; "
        "no hashtags, no links, no @mentions, no $tickers — those are added "
        "separately, do not write your own; at most one emoji; no financial "
        "advice and no hype like 'guaranteed' or 'to the moon'. "
        f"Output ONLY {SKIP_TOKEN} or the tweet text, nothing else."
    )
    return system, f"Facts: {fact}"


def clean_generated_body(raw: str | None, *, fact: str, budget: int) -> str | None:
    """Текст от модели, если ему можно доверить публикацию, иначе None.

    Проверки нарочно жёсткие и дешёвые: автопостинг публикует это без
    подтверждения владельцем, так что выдуманная цена или чужая ссылка
    не должны уйти в X вообще, а не просто "выглядеть подозрительно".
    Любой отказ вызывающий код превращает в шаблонный текст, а не в ошибку.
    """
    if not raw:
        return None

    body = " ".join(raw.split()).strip("\"'`“”«» ")
    if not body or len(body) > budget:
        return None

    lowered = body.lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        return None

    # Каждая цифра в твите должна быть в исходном факте — самая простая
    # защита от выдуманных цен, лимитов и количества кошельков.
    known = {_normalize_number(n) for n in _NUMBER.findall(fact)}
    if any(_normalize_number(n) not in known for n in _NUMBER.findall(body)):
        return None

    return body


# Чужой твит + автоматически прикреплённая карточка цитируемого твита —
# карточку вставляет сам X, но место под неё в лимите 280 всё равно уходит
# (как если бы в тексте была своя ссылка). Грубый, заведомо щедрый запас.
QUOTE_LINK_RESERVE = 25


def quote_budget() -> int:
    return MAX_LEN - QUOTE_LINK_RESERVE


def quote_prompt(tweet_text: str) -> tuple[str, str]:
    """(системная инструкция, сообщение) для комментария к чужому твиту.

    Решение "стоит ли вообще реагировать" и сам комментарий — один запрос,
    не два: отдельная проверка "стоит ли" перед запросом текста оказалась
    физически неподъёмной для локальной модели на этом железе (см.
    caption_prompt). Без шаблонного запасного пути (в отличие от неё) —
    цитату без комментария просить незачем. Нарочно не подтверждает чужие
    заявления как факт: это чужой твит, не проверенный он-чейн сигнал.
    """
    budget = quote_budget()
    system = (
        "You run an NFT-watcher account on X and want to add a short, "
        "genuine reaction when quote-tweeting someone else's post about a "
        "Robinhood Chain mint or whitelist. First, decide if this post is "
        "actually interesting enough to react to — real specifics or genuine "
        f"excitement, not a routine or templated-sounding announcement. If "
        f"it's generic or templated, reply with exactly {SKIP_TOKEN} and "
        "nothing else. Otherwise write ONE short comment. Don't confirm or "
        "restate claims from the post as verified fact — you haven't checked "
        "them yourself — react the way a curious, slightly skeptical person "
        "would: a quick take, a question, mild interest. "
        "Rules: English; at most "
        f"{budget} characters; base it ONLY on the text given — never invent "
        "numbers, prices, or claims not in it, and don't calculate new ones "
        "from it either; no hashtags, no links, no @mentions, no $tickers; "
        "at most one emoji; no financial advice. "
        f"Output ONLY {SKIP_TOKEN} or the comment text, nothing else."
    )
    return system, f"Post: {tweet_text}"
