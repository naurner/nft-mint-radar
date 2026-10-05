"""Адаптер Turso — против подставного libsql-клиента, без сети.

Проверяем, что db.py, написанный целиком под интерфейс aiosqlite,
работает через этот адаптер один в один: dict(row), cursor.lastrowid,
cursor.rowcount, async with conn.execute(...) as cur.
"""

from dataclasses import dataclass

from wlparser.db import Database
from wlparser.db_remote import RemoteConnection, split_statements

NFT = "0x" + "a" * 40


# --- split_statements ---


def test_splits_on_semicolons():
    script = "CREATE TABLE a (x INT);\nCREATE TABLE b (y INT);\n"
    assert split_statements(script) == [
        "CREATE TABLE a (x INT)",
        "CREATE TABLE b (y INT)",
    ]


def test_ignores_empty_chunks():
    assert split_statements(";;\n\n;") == []


def test_keeps_leading_comment_attached_to_its_statement():
    script = "-- comment\nCREATE TABLE a (x INT);\n"
    statements = split_statements(script)
    assert len(statements) == 1
    assert "CREATE TABLE a" in statements[0]


# --- фейковый libsql-клиент ---


@dataclass
class FakeRow:
    _columns: tuple
    _values: tuple

    def asdict(self):
        return dict(zip(self._columns, self._values))


class FakeResultSet:
    def __init__(self, columns, rows, *, rows_affected=0, last_insert_rowid=None):
        self.rows = [FakeRow(columns, r) for r in rows]
        self.rows_affected = rows_affected
        self.last_insert_rowid = last_insert_rowid


class FakeLibsqlClient:
    """Отвечает по таблице сценариев: подстрока SQL -> ResultSet."""

    def __init__(self, scripted=None, *, default=None):
        self.scripted = scripted or {}
        self.default = default or FakeResultSet((), [])
        self.calls: list[tuple[str, list]] = []
        self.closed = False

    async def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        for fragment, result in self.scripted.items():
            if fragment in sql:
                return result
        return self.default

    async def close(self):
        self.closed = True


async def test_execute_forwards_sql_and_params():
    client = FakeLibsqlClient()
    conn = RemoteConnection(client)
    await conn.execute("SELECT * FROM x WHERE a = ?", ("v",))
    assert client.calls == [("SELECT * FROM x WHERE a = ?", ["v"])]


async def test_fetchone_returns_a_plain_dict():
    client = FakeLibsqlClient(
        {"SELECT": FakeResultSet(("key", "value"), [("k1", "v1")])}
    )
    conn = RemoteConnection(client)
    async with conn.execute("SELECT key, value FROM cursors") as cur:
        row = await cur.fetchone()
    assert row == {"key": "k1", "value": "v1"}
    assert dict(row) == row  # код в db.py делает именно dict(row)


async def test_fetchone_is_none_for_empty_result():
    client = FakeLibsqlClient()
    conn = RemoteConnection(client)
    async with conn.execute("SELECT 1") as cur:
        assert await cur.fetchone() is None


async def test_fetchall_returns_every_row_as_dict():
    client = FakeLibsqlClient(
        {"SELECT": FakeResultSet(("id",), [(1,), (2,), (3,)])}
    )
    conn = RemoteConnection(client)
    async with conn.execute("SELECT id FROM signals") as cur:
        rows = await cur.fetchall()
    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]


async def test_execute_is_directly_awaitable_without_async_with():
    # db.py делает и так, и так — оба пути должны работать.
    client = FakeLibsqlClient()
    conn = RemoteConnection(client)
    cur = await conn.execute("INSERT INTO x VALUES (1)")
    assert await cur.fetchall() == []


async def test_lastrowid_is_exposed():
    client = FakeLibsqlClient({"INSERT": FakeResultSet((), [], last_insert_rowid=42)})
    conn = RemoteConnection(client)
    cur = await conn.execute("INSERT INTO reminders(...) VALUES (...)")
    assert cur.lastrowid == 42


async def test_rowcount_is_exposed():
    client = FakeLibsqlClient({"INSERT OR IGNORE": FakeResultSet((), [], rows_affected=1)})
    conn = RemoteConnection(client)
    cur = await conn.execute("INSERT OR IGNORE INTO seen(key) VALUES(?)", ("k",))
    assert cur.rowcount == 1


async def test_executescript_runs_each_statement_separately():
    client = FakeLibsqlClient()
    conn = RemoteConnection(client)
    await conn.executescript("CREATE TABLE a (x INT);\nCREATE TABLE b (y INT);\n")
    executed = [sql for sql, _ in client.calls]
    assert any("CREATE TABLE a" in s for s in executed)
    assert any("CREATE TABLE b" in s for s in executed)
    assert len(executed) == 2


async def test_commit_is_a_harmless_noop():
    conn = RemoteConnection(FakeLibsqlClient())
    await conn.commit()  # не должно бросить


async def test_close_closes_the_underlying_client():
    client = FakeLibsqlClient()
    await RemoteConnection(client).close()
    assert client.closed is True


# --- Database выбирает backend по виду пути ---


def test_local_path_does_not_touch_libsql():
    db = Database("data/wlparser.sqlite")
    assert db._path is not None


def test_libsql_url_is_recognized_as_remote():
    db = Database("libsql://example.turso.io", auth_token="tok")
    assert db._path is None


def test_https_url_is_also_treated_as_remote():
    # Turso поддерживает и https:// для того же сервера.
    db = Database("https://example.turso.io")
    assert db._path is None


async def test_database_works_end_to_end_against_the_fake_backend(monkeypatch):
    """Полный прогон через настоящий Database с адаптером вместо файла.

    Если что-то в SQL несовместимо с абстракцией курсора, это всплывёт
    здесь, а не в проде при первом запуске с реальным токеном.
    """
    client = FakeLibsqlClient()
    monkeypatch.setattr(
        "wlparser.db_remote.connect", lambda url, token: RemoteConnection(client)
    )

    db = Database("libsql://fake.turso.io", auth_token="tok")
    await db.connect()
    await db.set_cursor("block", "123")
    assert client.calls  # схема и запрос действительно ушли через адаптер
    await db.close()
    assert client.closed is True


def test_from_settings_picks_remote_when_url_is_set():
    from wlparser.config import Settings

    settings = Settings(db_url="libsql://x.turso.io", db_auth_token="tok")
    db = Database.from_settings(settings)
    assert db._path is None
    assert db._raw_path == "libsql://x.turso.io"
    assert db._auth_token == "tok"


def test_from_settings_falls_back_to_local_file():
    from wlparser.config import Settings

    settings = Settings(db_path="data/x.sqlite")
    db = Database.from_settings(settings)
    assert db._path is not None


# --- libsql:// -> https:// ---


def test_libsql_scheme_is_rewritten_to_https(monkeypatch):
    """Против настоящего Turso hrana/WebSocket-рукопожатие по libsql://
    не проходит (проверено вживую), а https:// с тем же хостом работает.
    Адрес нормализуется автоматически — владельцу не нужно об этом знать.
    """
    captured = {}

    class FakeLibsqlModule:
        @staticmethod
        def create_client(url, auth_token=None):
            captured["url"] = url
            captured["auth_token"] = auth_token
            return object()

    monkeypatch.setitem(__import__("sys").modules, "libsql_client", FakeLibsqlModule())

    from wlparser.db_remote import connect

    connect("libsql://example.turso.io", "tok")
    assert captured["url"] == "https://example.turso.io"
    assert captured["auth_token"] == "tok"


def test_https_scheme_passes_through_unchanged(monkeypatch):
    captured = {}

    class FakeLibsqlModule:
        @staticmethod
        def create_client(url, auth_token=None):
            captured["url"] = url
            return object()

    monkeypatch.setitem(__import__("sys").modules, "libsql_client", FakeLibsqlModule())

    from wlparser.db_remote import connect

    connect("https://example.turso.io", None)
    assert captured["url"] == "https://example.turso.io"
