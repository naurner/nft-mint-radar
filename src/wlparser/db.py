"""Хранилище SQLite. Единственное место, знающее про схему."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .models import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    address TEXT PRIMARY KEY,
    chain TEXT,
    name TEXT,
    symbol TEXT,
    standard TEXT,
    total_supply INTEGER,
    max_supply INTEGER,
    mint_price_wei INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    unique_minters INTEGER DEFAULT 0,
    telegram_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    url TEXT,
    author TEXT,
    address TEXT,
    chain TEXT,
    ts TEXT NOT NULL,
    score INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_address ON signals(address);

CREATE TABLE IF NOT EXISTS cursors (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    joined_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    is_owner INTEGER NOT NULL DEFAULT 0
);

-- Ветка обсуждения проекта своя в каждом чате: один и тот же контракт
-- получает разные message_id у владельца и у каждого подписчика.
CREATE TABLE IF NOT EXISTS threads (
    chat_id INTEGER NOT NULL,
    address TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, address)
);

-- Напоминания, поставленные вручную под конкретным алертом.
-- payload хранит текст исходного сообщения: напоминание дублирует его
-- целиком, чтобы не пришлось листать чат вверх в поисках оригинала.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    address TEXT,
    payload TEXT NOT NULL,
    note TEXT,
    due_at TEXT NOT NULL,
    target_at TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'lead',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(status, due_at);

-- Снимок состояния коллекции в момент времени: цена минта, сколько
-- сминчено из скольких, флор на вторичке. Ряд снимков одного адреса —
-- это история цены, из которой позже можно судить, стоила ли раздача
-- того, и на которой когда-нибудь можно будет обучить модель.
-- floor_price_eth хранится в ETH (как отдаёт OpenSea), а не в wei, как
-- остальные суммы, — чтобы не вносить ошибку округления пересчётом.
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    ts TEXT NOT NULL,
    total_supply INTEGER,
    max_supply INTEGER,
    mint_price_wei INTEGER,
    floor_price_eth REAL,
    unique_minters INTEGER,
    num_owners INTEGER,
    source TEXT
    -- floor_price_symbol доезжает через ALTER TABLE (см. _SNAPSHOT_COLUMNS):
    -- флор на OpenSea не всегда в ETH, бывает и в стейблкоине вроде USDG.
);
CREATE INDEX IF NOT EXISTS idx_snapshots_address_ts ON snapshots(address, ts);

-- Черновик поста в X, ждущий подтверждения владельца в Telegram, и уже
-- отправленные посты — вместе, чтобы одним запросом считать дневной лимит
-- и не постить один и тот же проект дважды.
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tweet_url TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status_created ON posts(status, created_at);
"""

# SQLite INTEGER — знаковое 64-битное число. Цена минта в контракте лежит
# в wei (1 ETH = 10**18), и уже 10 ETH это 10**19 — не влезает. То же самое
# с totalSupply/maxSupply: наши сигнатуры цены и лимита угадываются перебором
# по частым именам функций, и на контракте, где угадали неверно, ответом
# может прийти что угодно, вплоть до огромного мусорного значения.
_SQLITE_INT_MAX = 2**63 - 1
_SQLITE_INT_MIN = -(2**63)


def _safe_int(value: int | None) -> int | None:
    """None вместо значения, не влезающего в SQLite INTEGER.

    Число, которое физически не может там храниться, всё равно не несёт
    смысла — цена в 10**30 wei или лимит в 10**40 токенов это не более
    настоящая величина, чем отсутствие данных. Лучше явное «не знаем»,
    чем падение всего конвейера на конкретном контракте.
    """
    if value is None:
        return None
    if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        return value
    return None


# Колонки, добавленные после первого релиза. База у владельца уже создана,
# поэтому они доезжают через ALTER TABLE, а не через пересоздание схемы.
_PROJECT_COLUMNS = {
    "opensea_url": "TEXT",
    "website": "TEXT",
    "twitter": "TEXT",
    "discord": "TEXT",
    "telegram": "TEXT",
    "description": "TEXT",
    "opensea_slug": "TEXT",
    "tracking_status": "TEXT",
    "tracking_started_at": "TEXT",
    "last_checked_at": "TEXT",
    "stale_checks": "INTEGER DEFAULT 0",
    "interest_score": "INTEGER",
    "interest_reason": "TEXT",
    # Один раз за жизнь проекта: чтобы «почти распродано» не слалось на
    # каждом тике, пока прогресс держится выше порога.
    "near_sellout_alerted": "INTEGER DEFAULT 0",
}

# Аналогично для snapshots: столбец добавлен позже первого релиза таблицы.
_SNAPSHOT_COLUMNS = {
    "floor_price_symbol": "TEXT",
}

# Аналогично для posts: различает собственный пост о находке и
# цитирование чужого твита — у них разные квоты (см. poster/service.py)
# и разный смысл строки "предложено за сутки" в /menu → Статус.
# quoted_url — какой твит цитируем (нужен approve(), когда kind='quote').
_POST_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'post'",
    "quoted_url": "TEXT",
}


def _is_remote_url(path: str) -> bool:
    return path.startswith(("libsql://", "http://", "https://", "wss://"))


class Database:
    """Локальный SQLite-файл или общая база Turso — по виду пути.

    Запросы ниже написаны один раз против интерфейса aiosqlite; когда
    `path` выглядит как URL, вместо файла подключается db_remote.py —
    адаптер, подделывающий тот же интерфейс поверх libsql_client. Так
    бот может работать с одной базой независимо от того, на каком
    устройстве запущен, без двух наборов запросов.
    """

    def __init__(self, path: str | Path, *, auth_token: str | None = None):
        self._raw_path = str(path)
        self._path = None if _is_remote_url(self._raw_path) else Path(path)
        self._auth_token = auth_token
        self._conn = None

    @classmethod
    def from_settings(cls, settings) -> "Database":
        """Общая база, если задан `db_url`, иначе локальный файл `db_path`.

        Единая точка выбора backend'а: CLI и приложение не должны каждый
        по-своему решать, откуда брать соединение.
        """
        if settings.db_url:
            return cls(settings.db_url, auth_token=settings.db_auth_token)
        return cls(settings.db_path)

    async def connect(self) -> None:
        if self._path is None:
            from . import db_remote

            self._conn = db_remote.connect(self._raw_path, self._auth_token)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row

        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        await self._migrate_table("projects", _PROJECT_COLUMNS)
        await self._migrate_table("snapshots", _SNAPSHOT_COLUMNS)
        await self._migrate_table("posts", _POST_COLUMNS)

    async def _migrate_table(self, table: str, columns: dict[str, str]) -> None:
        async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for column, column_type in columns.items():
            if column not in existing:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # --- перенос базы (Turso -> локальный файл, если кончился лимит) ---

    _TABLES = (
        "subscribers",
        "cursors",
        "seen",
        "projects",
        "signals",
        "threads",
        "reminders",
        "snapshots",
        "posts",
    )

    async def copy_all_rows_to(self, destination: "Database") -> dict[str, int]:
        """Копирует все строки из этой базы в другую, таблица за таблицей.

        Обе базы должны быть уже подключены (.connect() сам создаёт и
        мигрирует схему, так что колонки совпадают). INSERT OR IGNORE —
        если в destination уже что-то есть (бот успел поработать на
        пустой локальной базе до переноса), совпавшие по PRIMARY KEY
        строки просто не трогаем, а не роняем весь перенос на первом
        конфликте.
        """
        counts: dict[str, int] = {}
        for table in self._TABLES:
            async with self._conn.execute(f"SELECT * FROM {table}") as cur:
                rows = [dict(row) for row in await cur.fetchall()]
            for row in rows:
                columns = list(row.keys())
                placeholders = ",".join("?" for _ in columns)
                await destination._conn.execute(
                    f"INSERT OR IGNORE INTO {table}({','.join(columns)})"
                    f" VALUES({placeholders})",
                    [row[c] for c in columns],
                )
            await destination._conn.commit()
            counts[table] = len(rows)
        return counts

    # --- курсоры ---

    async def get_cursor(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM cursors WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_cursor(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO cursors(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # --- дедупликация ---

    async def mark_seen(self, key: str) -> bool:
        """True, если ключ встретился впервые."""
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO seen(key) VALUES(?)", (key,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # --- сигналы и проекты ---

    async def record_signal(self, signal: Signal, score: int) -> None:
        ts = signal.timestamp.isoformat()
        if signal.contract_address:
            info = signal.raw.get("info") or {}
            await self._conn.execute(
                "INSERT INTO projects(address, chain, name, symbol, standard,"
                " total_supply, max_supply, mint_price_wei, first_seen, last_seen)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(address) DO UPDATE SET"
                "   last_seen = excluded.last_seen,"
                "   name = COALESCE(projects.name, excluded.name),"
                "   symbol = COALESCE(projects.symbol, excluded.symbol),"
                "   standard = COALESCE(projects.standard, excluded.standard),"
                "   max_supply = COALESCE(projects.max_supply, excluded.max_supply),"
                "   mint_price_wei = COALESCE(projects.mint_price_wei,"
                "                             excluded.mint_price_wei)",
                (
                    signal.contract_address,
                    signal.chain,
                    info.get("name"),
                    info.get("symbol"),
                    info.get("standard"),
                    _safe_int(info.get("total_supply")),
                    _safe_int(info.get("max_supply")),
                    _safe_int(info.get("mint_price_wei")),
                    ts,
                    ts,
                ),
            )
        await self._conn.execute(
            "INSERT INTO signals(source, kind, text, url, author, address,"
            " chain, ts, score, raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                signal.source,
                signal.kind,
                signal.text,
                signal.url,
                signal.author,
                signal.contract_address,
                signal.chain,
                ts,
                score,
                json.dumps(signal.raw, default=str),
            ),
        )
        await self._conn.commit()

    async def get_project(self, address: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM projects WHERE address = ?", (address,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # --- ветки сообщений (свои в каждом чате) ---

    async def set_thread_id(self, chat_id: int, address: str, message_id: int) -> None:
        await self._conn.execute(
            "INSERT INTO threads(chat_id, address, message_id) VALUES(?,?,?)"
            " ON CONFLICT(chat_id, address) DO UPDATE SET"
            " message_id = excluded.message_id",
            (int(chat_id), address, message_id),
        )
        await self._conn.commit()

    async def load_threads(self) -> dict[tuple[int, str], int]:
        """Ветки Telegram, чтобы после перезапуска продолжать существующие."""
        async with self._conn.execute(
            "SELECT chat_id, address, message_id FROM threads"
        ) as cur:
            rows = await cur.fetchall()
        return {(row["chat_id"], row["address"]): row["message_id"] for row in rows}

    async def threads_for_address(self, address: str) -> list[tuple[int, int]]:
        """Все (чат, сообщение) для проекта — сюда правится живая карточка."""
        async with self._conn.execute(
            "SELECT chat_id, message_id FROM threads WHERE address = ?", (address,)
        ) as cur:
            rows = await cur.fetchall()
        return [(row["chat_id"], row["message_id"]) for row in rows]

    async def delete_thread(self, chat_id: int, address: str) -> None:
        """Убирает ветку, если её сообщение удалено человеком в Telegram.

        Иначе слежение продолжало бы биться в закрытую дверь на каждом тике.
        """
        await self._conn.execute(
            "DELETE FROM threads WHERE chat_id = ? AND address = ?",
            (int(chat_id), address),
        )
        await self._conn.commit()

    # --- подписчики ---

    async def add_subscriber(
        self,
        chat_id: int,
        *,
        username: str | None = None,
        display_name: str | None = None,
        is_owner: bool = False,
    ) -> bool:
        """Регистрирует чат. True — если человек пришёл впервые.

        Вернувшийся после /stop не считается новым: владельцу не нужно
        уведомление о каждом повторном заходе одного и того же человека.
        """
        async with self._conn.execute(
            "SELECT chat_id FROM subscribers WHERE chat_id = ?", (int(chat_id),)
        ) as cur:
            existed = await cur.fetchone() is not None

        await self._conn.execute(
            "INSERT INTO subscribers(chat_id, username, display_name, joined_at,"
            " active, is_owner) VALUES(?,?,?,?,1,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            "   active = 1,"
            "   username = COALESCE(excluded.username, subscribers.username),"
            "   display_name = COALESCE(excluded.display_name,"
            "                           subscribers.display_name),"
            "   is_owner = MAX(subscribers.is_owner, excluded.is_owner)",
            (
                int(chat_id),
                username,
                display_name,
                datetime.now(timezone.utc).isoformat(),
                1 if is_owner else 0,
            ),
        )
        await self._conn.commit()
        return not existed

    async def set_subscriber_active(self, chat_id: int, active: bool) -> None:
        await self._conn.execute(
            "UPDATE subscribers SET active = ? WHERE chat_id = ?",
            (1 if active else 0, int(chat_id)),
        )
        await self._conn.commit()

    async def list_subscribers(self, *, active_only: bool = True) -> list[dict]:
        query = "SELECT * FROM subscribers"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY is_owner DESC, joined_at"
        async with self._conn.execute(query) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def count_subscribers(self) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM subscribers WHERE active = 1"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def save_links(self, address: str, links) -> None:
        """Кеширует найденные ссылки, чтобы не ходить в OpenSea повторно."""
        await self._conn.execute(
            "UPDATE projects SET opensea_url = COALESCE(?, opensea_url),"
            " website = COALESCE(?, website), twitter = COALESCE(?, twitter),"
            " discord = COALESCE(?, discord), telegram = COALESCE(?, telegram),"
            " description = COALESCE(?, description),"
            " opensea_slug = COALESCE(?, opensea_slug),"
            " name = COALESCE(name, ?) WHERE address = ?",
            (
                links.opensea,
                links.website,
                links.twitter,
                links.discord,
                links.telegram,
                links.description,
                links.collection_slug,
                links.collection_name,
                address,
            ),
        )
        await self._conn.commit()

    # --- слежение за коллекцией (живые снимки цены и прогресса) ---

    async def start_tracking(self, address: str) -> bool:
        """Включает слежение за проектом. True — если запущено впервые.

        Повторный вызов для уже отслеживаемого проекта ничего не делает:
        так безопасно вызывать это на каждый сигнал по адресу, а не следить
        отдельно, отправлялся ли уже алерт.
        """
        row = await self.get_project(address)
        if row is None or row.get("tracking_started_at"):
            return False
        await self._conn.execute(
            "UPDATE projects SET tracking_started_at = ?, tracking_status = 'active',"
            " stale_checks = 0 WHERE address = ?",
            (datetime.now(timezone.utc).isoformat(), address),
        )
        await self._conn.commit()
        return True

    async def active_tracked_projects(self) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM projects WHERE tracking_status = 'active'"
            " AND tracking_started_at IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def set_tracking_status(self, address: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE projects SET tracking_status = ?, last_checked_at = ?"
            " WHERE address = ?",
            (status, datetime.now(timezone.utc).isoformat(), address),
        )
        await self._conn.commit()

    async def bump_stale_checks(self, address: str) -> int:
        """Считает подряд идущие проверки без изменений. Возвращает счётчик."""
        await self._conn.execute(
            "UPDATE projects SET stale_checks = stale_checks + 1,"
            " last_checked_at = ? WHERE address = ?",
            (datetime.now(timezone.utc).isoformat(), address),
        )
        await self._conn.commit()
        row = await self.get_project(address)
        return row["stale_checks"] if row else 0

    async def reset_stale_checks(self, address: str) -> None:
        await self._conn.execute(
            "UPDATE projects SET stale_checks = 0, last_checked_at = ?"
            " WHERE address = ?",
            (datetime.now(timezone.utc).isoformat(), address),
        )
        await self._conn.commit()

    async def set_opensea_slug(self, address: str, slug: str) -> None:
        await self._conn.execute(
            "UPDATE projects SET opensea_slug = COALESCE(opensea_slug, ?)"
            " WHERE address = ?",
            (slug, address),
        )
        await self._conn.commit()

    async def set_interest_score(self, address: str, score: int, reason: str) -> None:
        await self._conn.execute(
            "UPDATE projects SET interest_score = ?, interest_reason = ?"
            " WHERE address = ?",
            (score, reason, address),
        )
        await self._conn.commit()

    async def mark_near_sellout_alerted(self, address: str) -> None:
        """Разово отмечает, что об этом проекте уже сообщили про приближение

        к распродаже — иначе алерт повторялся бы на каждом тике, пока
        прогресс держится выше порога.
        """
        await self._conn.execute(
            "UPDATE projects SET near_sellout_alerted = 1 WHERE address = ?",
            (address,),
        )
        await self._conn.commit()

    async def search_projects(self, query: str, limit: int = 5) -> list[dict]:
        """Поиск по имени/тикеру среди уже найденных проектов.

        Не поиск по всему OpenSea — только по тому, что бот сам уже нашёл
        в сети или в твиттере. Регистр не важен: LIKE в SQLite по умолчанию
        не различает его для ASCII.
        """
        pattern = f"%{query.strip()}%"
        async with self._conn.execute(
            "SELECT * FROM projects WHERE name LIKE ? OR symbol LIKE ?"
            " ORDER BY last_seen DESC LIMIT ?",
            (pattern, pattern, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def top_projects(self, limit: int = 10, *, within_days: float = 14.0) -> list[dict]:
        """Без окна по времени рейтинг только рос бы: раздачи, о которых

        давно ничего не слышно, никуда из топа не исчезали бы и заслоняли
        собой то, что происходит прямо сейчас. last_seen обновляется, пока
        идёт слежение, так что активные проекты остаются в топе сами, а
        неактивные через within_days выпадают без отдельной чистки.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        async with self._conn.execute(
            "SELECT * FROM projects WHERE interest_score IS NOT NULL AND last_seen >= ?"
            " ORDER BY interest_score DESC LIMIT ?",
            (since, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # --- снимки истории (датасет для будущей модели) ---

    async def add_snapshot(
        self,
        *,
        address: str,
        ts: datetime,
        total_supply: int | None = None,
        max_supply: int | None = None,
        mint_price_wei: int | None = None,
        floor_price_eth: float | None = None,
        floor_price_symbol: str | None = None,
        unique_minters: int | None = None,
        num_owners: int | None = None,
        source: str = "tracker",
    ) -> None:
        await self._conn.execute(
            "INSERT INTO snapshots(address, ts, total_supply, max_supply,"
            " mint_price_wei, floor_price_eth, floor_price_symbol, unique_minters,"
            " num_owners, source) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                address,
                ts.isoformat(),
                _safe_int(total_supply),
                _safe_int(max_supply),
                _safe_int(mint_price_wei),
                floor_price_eth,
                floor_price_symbol,
                unique_minters,
                num_owners,
                source,
            ),
        )
        await self._conn.commit()

    async def latest_snapshot(self, address: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM snapshots WHERE address = ? ORDER BY id DESC LIMIT 1",
            (address,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def snapshot_history(self, address: str, limit: int = 500) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM snapshots WHERE address = ? ORDER BY id ASC LIMIT ?",
            (address, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # --- напоминания ---

    async def add_reminder(
        self,
        *,
        chat_id: int,
        payload: str,
        due_at: datetime,
        target_at: datetime,
        address: str | None = None,
        note: str | None = None,
        stage: str = "lead",
    ) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO reminders(chat_id, address, payload, note, due_at,"
            " target_at, stage, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?)",
            (
                int(chat_id),
                address,
                payload,
                note,
                due_at.isoformat(),
                target_at.isoformat(),
                stage,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_reminder(self, reminder_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (int(reminder_id),)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def due_reminders(self, now: datetime) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ?"
            " ORDER BY due_at",
            (now.isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def set_reminder_status(self, reminder_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE reminders SET status = ? WHERE id = ?",
            (status, int(reminder_id)),
        )
        await self._conn.commit()

    async def set_reminder_note(self, reminder_id: int, note: str) -> None:
        await self._conn.execute(
            "UPDATE reminders SET note = ? WHERE id = ?", (note, int(reminder_id))
        )
        await self._conn.commit()

    async def pending_reminders(self, chat_id: int) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM reminders WHERE chat_id = ? AND status = 'pending'"
            " ORDER BY due_at",
            (int(chat_id),),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # --- посты в X ---

    async def create_pending_post(
        self,
        *,
        address: str | None,
        text: str,
        kind: str = "post",
        quoted_url: str | None = None,
    ) -> int:
        """kind: 'post' — собственная находка, 'quote' — цитирование чужого

        твита (см. poster/service.py) — у них разные квоты и разный смысл
        в статистике, хотя обрабатываются тем же статусом pending/posted.
        quoted_url — какой твит цитируем, только для kind='quote'.
        """
        cursor = await self._conn.execute(
            "INSERT INTO posts(address, text, status, created_at, kind, quoted_url)"
            " VALUES(?,?,'pending',?,?,?)",
            (address, text, datetime.now(timezone.utc).isoformat(), kind, quoted_url),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_post(self, post_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM posts WHERE id = ?", (int(post_id),)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def decide_post(
        self, post_id: int, *, status: str, tweet_url: str | None = None
    ) -> None:
        """Переводит черновик в конечный статус — 'posted' или 'skipped'.

        Однократно: если пост уже решён (проверяется вызывающим кодом до
        обращения к твиттеру), повторное нажатие кнопки не должно постить
        дважды.
        """
        await self._conn.execute(
            "UPDATE posts SET status = ?, tweet_url = ?, decided_at = ?"
            " WHERE id = ?",
            (status, tweet_url, datetime.now(timezone.utc).isoformat(), int(post_id)),
        )
        await self._conn.commit()

    async def count_posts_since(self, since: datetime, *, kind: str = "post") -> int:
        """Сколько постов этого kind реально ушло в X — для квоты и статуса.

        kind='post' — собственные находки (Poster._post_quota_used),
        kind='quote' — цитирования чужих твитов (Poster._quote_quota_used):
        у них разные дневные лимиты, поэтому и считаются раздельно.
        """
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM posts"
            " WHERE status = 'posted' AND decided_at >= ? AND kind = ?",
            (since.isoformat(), kind),
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0

    async def count_drafts_since(self, since: datetime, *, kind: str = "post") -> int:
        """Сколько черновиков от РЕАЛЬНЫХ находок предложено с этого момента —

        любой статус (pending/posted/skipped). Дневной лимит на сам поток
        предложений владельцу, а не только на то, что он успел одобрить.

        Ручные тестовые черновики (send_test_draft, address=None — у
        находок с сигнала всегда есть адрес контракта) сюда не считаются:
        владелец создаёт их сам, осознанно, чтобы что-то проверить, и они
        не должны отъедать лимит у настоящих находок — ровно это и
        случилось при отладке публикации, когда несколько тестовых постов
        забили лимit на весь день, и автопост замолчал не из-за бага.
        """
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM posts"
            " WHERE created_at >= ? AND address IS NOT NULL AND kind = ?",
            (since.isoformat(), kind),
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0

    async def count_pending_posts(self) -> int:
        """Сколько черновиков сейчас ждёт решения владельца в Telegram."""
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0

    async def clear_pending_posts(self) -> int:
        """Списывает все висящие черновики как отменённые — ничего не постит.

        Для случая, когда очередь предложений скопилась (например, публикация
        в X сама не работает), и владелец хочет расчистить её разом. Старые
        кнопки "Опубликовать"/"Пропустить" под уже отправленными в Telegram
        черновиками после этого просто ответят, что черновик не актуален —
        decide_post их не удаляет, а помечает решёнными.
        """
        cursor = await self._conn.execute(
            "UPDATE posts SET status='skipped', decided_at=? WHERE status='pending'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def recent_signals(self, limit: int = 20) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # --- периодический дайджест (см. notify/digest.py) ---

    async def digest_projects_since(self, since: datetime, limit: int = 8) -> list[dict]:
        """Проекты, замеченные с прошлого дайджеста, лучшие по interest_score

        первыми. "(interest_score IS NULL)" сортирует False (0) перед True
        (1) — то есть посчитанные раньше непосчитанных, без ORDER BY ...
        NULLS LAST, которого не было в SQLite до версии 3.30.
        """
        async with self._conn.execute(
            "SELECT * FROM projects WHERE last_seen >= ?"
            " ORDER BY (interest_score IS NULL), interest_score DESC, last_seen DESC"
            " LIMIT ?",
            (since.isoformat(), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def digest_announcements_since(self, since: datetime, limit: int = 5) -> list[dict]:
        """Анонсы WL в твиттере с прошлого дайджеста, самые свежие первыми."""
        async with self._conn.execute(
            "SELECT * FROM signals WHERE kind = 'wl_announced' AND ts >= ?"
            " ORDER BY ts DESC LIMIT ?",
            (since.isoformat(), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]
