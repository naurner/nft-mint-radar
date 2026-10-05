"""Конфигурация: YAML для настроек, .env для секретов.

Разделение намеренное: config.yaml можно показывать и коммитить,
а всё, что нельзя, живёт в .env и не попадает в репозиторий.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# Пути переопределяются окружением: в контейнере и конфиг, и секреты должны
# лежать в примонтированном томе, иначе настройки исчезнут при перезапуске.
DEFAULT_CONFIG_PATH = Path(os.getenv("WLPARSER_CONFIG") or "config/config.yaml")
DEFAULT_ENV_PATH = Path(os.getenv("WLPARSER_ENV") or ".env")
DEFAULT_EXPLORER = "https://robinscan.io"


class ChainSettings(BaseModel):
    name: str = "robinhood"
    rpc_url: str = ""
    # Запасные узлы: запросы раскладываются по кругу, лимит частоты
    # считается отдельно на каждом, и падение одного не останавливает бота.
    rpc_urls: list[str] = Field(default_factory=list)
    chain_id: int | None = None
    explorer_url: str = DEFAULT_EXPLORER
    # У разных эксплореров разные пути к странице контракта.
    explorer_token_path: str = "/token/{address}"
    poll_interval_s: float = 2.0
    surge_threshold: int = 25
    confirmations: int = 2
    # Сеть выдаёт около десяти блоков в секунду, поэтому логи запрашиваются
    # диапазонами. Эндпоинт отклоняет запросы шире 1000 блоков.
    log_range_blocks: int = 500
    # Отставание больше этого не догоняется: те раздачи давно закончились,
    # а пока мы их разбираем, пропускаем происходящее сейчас.
    max_lag_blocks: int = 5000
    # Пауза между запросами к RPC, чтобы не ловить 429 на публичном узле.
    min_request_interval_s: float = 0.25

    @property
    def endpoints(self) -> list[str]:
        seen = {}
        for url in [self.rpc_url, *self.rpc_urls]:
            if url:
                seen.setdefault(url, None)
        return list(seen)

    @field_validator("explorer_url")
    @classmethod
    def _fall_back_to_default_explorer(cls, value: str) -> str:
        # Мастер настройки разрешает пропустить этот вопрос, и в конфиге
        # остаётся пустая строка. Ссылки в алертах нужны в любом случае.
        return value.strip() or DEFAULT_EXPLORER


class LinkSettings(BaseModel):
    """Подтягивание ссылок и соцсетей проекта.

    Публичные эндпоинты OpenSea отвечают без ключа, поэтому по умолчанию
    всё работает без настройки. Ключ нужен только для повышения лимитов.
    """

    opensea_enabled: bool = True
    opensea_chain: str = "robinhood"
    opensea_api_key: str = ""
    timeout_s: float = 8.0


class AlertSettings(BaseModel):
    min_score: int = 45
    # Отслеживание деплоев требовало забирать каждый блок целиком, что при
    # десяти блоках в секунду публичный RPC не выдерживает. Новые коллекции
    # теперь обнаруживаются по первым минтам — раньше, чем они станут
    # заметны, и без нагрузки на узел.
    notify_deploys: bool = False
    notify_mints: bool = True
    # Фора владельца перед остальными подписчиками, в секундах.
    # При публичном боте решает, кто увидит находку первым. 0 — все разом.
    owner_head_start_s: float = 0.0
    # За сколько минут до события приходит напоминание.
    reminder_lead_minutes: int = 10


class NoiseSettings(BaseModel):
    """Отсев лишнего перед отправкой.

    Пороги подобраны по реальному потоку: шаблонные посты за активность,
    множество сообщений про один проект и просто слишком много всего сразу.
    """

    # Доля общих слов, при которой тексты считаются одним шаблоном.
    similarity_threshold: float = 0.7
    text_window_hours: float = 12.0
    # Сколько молчать про проект после того, как о нём уже сообщили.
    topic_cooldown_minutes: float = 240.0
    # То же для он-чейн событий: «начался минт» и «всплеск» по одному
    # контракту — это одно событие, пересказанное дважды.
    onchain_topic_cooldown_minutes: float = 30.0
    # Жёсткий потолок: больше этого в час не придёт ни при каких условиях,
    # кроме по-настоящему горячих находок.
    max_alerts_per_hour: int = 8
    min_author_followers: int = 100
    # Оценка, начиная с которой сигнал проходит мимо ограничений.
    urgent_score: int = 85


class MintSettings(BaseModel):
    """Порог, отделяющий раздачу от шума.

    Первый минт ничего не значит — так выглядит и тест разработчика.
    Раздачу отличает количество разных кошельков за короткое время.
    """

    min_unique_minters: int = 25
    window_minutes: int = 120
    ignore_contracts: list[str] = Field(default_factory=list)
    # None означает «взять список по умолчанию» из filters.DEFAULT_NAME_PATTERNS.
    ignore_name_patterns: list[str] | None = None


DEFAULT_QUERIES = [
    '"robinhood chain" (whitelist OR WL OR mint) -filter:retweets',
    "robinhoodchain (mint OR whitelist OR WL) -filter:retweets",
    '"robinhood chain" nft (drop OR launch OR live) -filter:retweets',
    "#RobinhoodChain -filter:retweets",
]


class TrackingSettings(BaseModel):
    """Живая карточка коллекции: цена, прогресс минта и флор в одном
    сообщении, которое обновляется на месте вместо новых сообщений.

    Цена минта и прогресс сминченного читаются из сети напрямую и работают
    без единого внешнего ключа. Флор и число держателей — из OpenSea,
    и появятся только если задан links.opensea_api_key.
    """

    enabled: bool = True
    # Обычный темп проверки одного проекта. Настоящая частота у каждого
    # проекта своя и зависит от того, сколько уже разобрано (см.
    # tracking/service.py::_FILL_INTERVAL_TIERS) — это лишь базовая ставка.
    refresh_interval_s: float = 300.0
    # Как часто цикл вообще просыпается посмотреть, кому пора проверяться.
    # Короче refresh_interval_s специально: коллекции у порога распродажи
    # проверяются чаще базовой ставки, а для этого цикл должен просыпаться
    # чаще неё, а не только с её периодом.
    poll_interval_s: float = 60.0
    # Дольше этого коллекцию не отслеживаем. Было 48 часов — коротко для
    # сбора истории (владелец попросил собирать максимум данных, раз база
    # теперь локальная и без лимитов); 30 дней вместо этого. Не удорожает
    # опрос заброшенных проектов: те отваливаются раньше сами через
    # stale_after_checks, а этот срок реально сработает только для
    # проектов, которые всё ещё меняются спустя 48 часов.
    max_duration_hours: float = 720.0
    # Если цифры не меняются столько проверок подряд, слежение
    # останавливается само.
    stale_after_checks: int = 12
    # Пауза между проверками разных проектов в одном тике. RPC делит лимит
    # запросов на всех: без паузы каждый тик — это залп из десятков вызовов
    # подряд (инспекция + цена на контракт), который сеть режет как всплеск
    # с кодом 429, и часть карточек в этом тике молча не обновляется.
    check_delay_s: float = 0.5
    # Как часто заново спрашивать у OpenSea последнюю/макс. продажу, в
    # секундах. Эти цифры не обязаны быть свежее пяти минут, а отдельный
    # запрос на КАЖДЫЙ тик КАЖДОГО проекта удваивает нагрузку на лимит
    # частоты OpenSea сверх того, что уже уходит на флор и объём.
    sales_cache_ttl_s: float = 900.0
    # Резервный источник цены минта, когда контракт её не выдал ни одним из
    # угадываемых геттеров (частый случай для free-mint/allowlist-контрактов):
    # грубый поиск по сайту проекта на слова "price"/"mint price" рядом с
    # числом в ETH. Не надёжный парсер, а эвристика на лучший случай — если
    # не найдёт, просто не покажет цену, как и раньше.
    website_price_fallback_enabled: bool = True
    # Сайт не меняет цену минта посреди раздачи — нет смысла скрести его
    # каждый тик, кешируем надолго.
    website_price_cache_ttl_s: float = 3600.0
    # Доля от максимального саплая, при пересечении которой владельцу и
    # подписчикам приходит отдельное уведомление "почти распродано" — не
    # просто правка карточки (её Telegram не показывает как уведомление),
    # а такой же полноценный алерт, как "начался минт". Шлётся не больше
    # одного раза за жизнь проекта.
    # Было 0.9 — владелец жаловался, что часто приходит уже постфактум:
    # частота самих проверок тоже растёт только с этого порога (см.
    # _FILL_INTERVAL_TIERS в tracking/service.py), а быстрые раздачи
    # (сотни минтов за пару минут) успевают проскочить с 50% до 100%
    # между двумя редкими проверками на низком заполнении. 0.75 всё ещё
    # попадает в тир 0.50 (проверки каждые ~5 минут, не тот редкий тир
    # ниже), но не так рано, как 0.6 — владелец попросил заметнее ближе
    # к концу, а не почти на середине раздачи.
    near_sellout_threshold: float = 0.75


class PricingSettings(BaseModel):
    """Перевод цены минта в доллары — по желанию, необязательно.

    Курс ETH/USD — с CoinGecko, без ключа. Комиссия сети — грубая оценка
    (не точный расчёт под конкретный контракт): текущая gasPrice сети,
    умноженная на условный расход газа на минт.
    """

    enabled: bool = True
    refresh_interval_s: float = 180.0
    include_gas: bool = True
    # Условный расход газа на транзакцию минта. У разных контрактов сильно
    # разный — это не точная величина, а разумная оценка по умолчанию.
    gas_per_mint: int = 150_000


class TwitterSettings(BaseModel):
    """Скрапинг под личным аккаунтом, только чтение.

    Интервалы намеренно неровные и с разбросом: ровный опрос раз в минуту
    выглядит машинным и повышает шанс словить блокировку аккаунта.
    """

    enabled: bool = False
    queries: list[str] = Field(default_factory=lambda: list(DEFAULT_QUERIES))
    watchlist: list[str] = Field(default_factory=list)
    search_interval_s: float = 240.0
    watchlist_interval_s: float = 75.0
    per_query_limit: int = 20
    accounts_db: str = "data/twscrape_accounts.db"
    # Каким аккаунтом из accounts_db постить в X. Пусто — автопост выключен,
    # даже если poster.enabled — постить чужим/случайным аккаунтом не надо.
    post_as: str = ""


class PosterSettings(BaseModel):
    """Автопост находок в X — только собственные он-чейн факты, никогда

    чужой текст из твиттера как собственный пост (см. poster/caption.py),
    и публикуется сам, без подтверждения владельцем. Владелец явно
    попросил снять счётный лимит и вместо него отбирать по качеству: ИИ
    сам решает, достаточно ли находка заметна, чтобы о ней писать — не
    любой всплеск/почти-распродано, а только то, что выглядит реально
    интересным (решение зашито в тот же запрос, что пишет текст — см.
    poster/caption.py::SKIP_TOKEN). Если модель настроена, но упала или
    не ответила вовремя, — пропускает находку, а не публикует всё
    подряд: заметность важнее охвата, раз лимита по количеству больше
    нет. Если ИИ не настроен вовсе, порог просто неприменим — идёт
    обычный шаблонный текст, как и до появления этой проверки.
    """

    enabled: bool = False
    # None — без ограничения по числу: количество отбирает качественный
    # порог выше, а не счётчик. Число — дополнительный потолок в сутки,
    # даже если интересных находок было больше.
    max_posts_per_day: int | None = None
    # Отдельный, более консервативный лимит на цитирования чужих твитов
    # (см. Poster.maybe_quote) — новая, более рискованная с точки зрения
    # репутации активность (это уже не собственный факт, а комментарий к
    # чужому посту), поэтому по умолчанию ограничена, даже когда у
    # обычных постов лимита нет.
    max_quotes_per_day: int | None = 5


class LLMSettings(BaseModel):
    """Локальная модель на Маке (Ollama) — бесплатно и без ключей.

    Тот же способ подключения, что в соседнем проекте Lalascaner: Ollama
    стоит на самом Маке, а из контейнера до неё достаёт host.docker.internal.
    Включена по умолчанию, потому что ничего не ломает: пока модель не
    отвечает, тексты постов остаются шаблонными, а в /menu → Статус видно,
    жива ли она.
    """

    enabled: bool = True
    base_url: str = "http://host.docker.internal:11434/v1"
    # Пусто — взять первую подходящую из скачанных в Ollama (`ollama list`).
    model: str = ""
    api_key: str = "not-needed"
    # Генерация на своём железе бывает небыстрой — запас с избытком.
    timeout_s: float = 90.0
    # Финальная проверка каждого алерта на спам/мусорную коллекцию (см.
    # quality.py) — рискованнее генерации текста постов: ошибка там портит
    # только черновик твита, а здесь может молча скрыть настоящую находку.
    # Отдельный флаг, чтобы можно было выключить только эту часть, оставив
    # генерацию текста постов работать на той же модели.
    filter_alerts: bool = True


class DigestSettings(BaseModel):
    """Периодический дайджест вместо мгновенного алерта на каждую находку.

    Мгновенно всё равно уходит срочное — всплеск минта и почти
    распродано (см. pipeline/runner.py::Pipeline.URGENT_KINDS), там счёт
    на минуты. Всё остальное копится в базе и приходит пачкой в это
    время — владелец сам попросил не будить его на каждый анонс, но и не
    раз в сутки, а почаще. По умолчанию — раз в два часа, круглые сутки.
    Время — локальное, часовой пояс контейнера (переменная TZ, см.
    docker-compose.yml), а не UTC.
    """

    enabled: bool = True
    times: list[str] = Field(
        default_factory=lambda: [f"{h:02d}:00" for h in range(0, 24, 2)]
    )


class Settings(BaseModel):
    chain: ChainSettings = Field(default_factory=ChainSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    mint: MintSettings = Field(default_factory=MintSettings)
    noise: NoiseSettings = Field(default_factory=NoiseSettings)
    links: LinkSettings = Field(default_factory=LinkSettings)
    tracking: TrackingSettings = Field(default_factory=TrackingSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    twitter: TwitterSettings = Field(default_factory=TwitterSettings)
    poster: PosterSettings = Field(default_factory=PosterSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    db_path: str = "data/wlparser.sqlite"
    # Общая база на любом устройстве: libsql://<имя>.turso.io. Пустая
    # строка — работаем на локальном файле db_path, как раньше.
    db_url: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Токен доступа к общей базе — секрет, живёт в .env, не в config.yaml.
    db_auth_token: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.chain.rpc_url)


def load_env_file(path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Читает .env без внешней зависимости. Уже заданные переменные не трогает."""
    path = Path(path)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(
    path: str | Path = DEFAULT_CONFIG_PATH,
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> Settings:
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    env = load_env_file(env_path)
    data.setdefault(
        "telegram_bot_token",
        os.getenv("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", ""),
    )
    data.setdefault(
        "telegram_chat_id",
        os.getenv("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", ""),
    )
    data.setdefault(
        "db_auth_token",
        os.getenv("DB_AUTH_TOKEN") or env.get("DB_AUTH_TOKEN", ""),
    )
    return Settings(**data)
