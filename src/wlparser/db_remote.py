"""Адаптер Turso (libSQL) под тот же интерфейс, что даёт aiosqlite.

`Database` в db.py написан против aiosqlite: `async with conn.execute(...) as cur`,
`dict(row)`, `cursor.lastrowid`, `cursor.rowcount`. Чтобы держать общую базу на
любом устройстве, вместо переписывания всех запросов здесь собран адаптер,
подделывающий ровно эту часть API поверх libsql_client — общего с локальным
файлом кода запросов в db.py становится ноль строк для изменения.

Ключевое отличие: libsql_client.Row — не мэппинг (dict(row) на нём падает),
поэтому курсор здесь сразу превращает каждую строку в обычный dict через
row.asdict() — весь остальной код продолжает работать как раньше.
"""

from __future__ import annotations

from typing import Any


def split_statements(script: str) -> list[str]:
    """Режет схему на отдельные выражения для батча.

    executescript() у aiosqlite умеет несколько выражений в одной строке,
    libsql — нет. Простое разбиение по `;` безопасно для нашей схемы:
    строковых литералов с `;` внутри в ней нет, а комментарии `--` не
    мешают — SQL-парсер игнорирует их в начале выражения.
    """
    return [chunk.strip() for chunk in script.split(";") if chunk.strip()]


class _RemoteCursor:
    """Оборачивает ResultSet так, чтобы `async with conn.execute() as cur` работал."""

    def __init__(self, result):
        self._rows = [row.asdict() for row in result.rows]
        self.rowcount = result.rows_affected
        self.lastrowid = result.last_insert_rowid

    async def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict]:
        return self._rows

    async def __aenter__(self) -> "_RemoteCursor":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None


class _ExecuteAwaitable:
    """`execute()` у aiosqlite одновременно awaitable и async context manager —
    повторяем оба варианта использования, встречающихся в db.py."""

    def __init__(self, client, sql: str, params: tuple | list):
        self._client = client
        self._sql = sql
        self._params = list(params) if params else []

    async def _run(self) -> _RemoteCursor:
        result = await self._client.execute(self._sql, self._params)
        return _RemoteCursor(result)

    def __await__(self):
        return self._run().__await__()

    async def __aenter__(self) -> _RemoteCursor:
        self._cursor = await self._run()
        return self._cursor

    async def __aexit__(self, *exc_info) -> None:
        return None


class RemoteConnection:
    """То же поведение, что db.py ждёт от aiosqlite.Connection."""

    def __init__(self, client):
        self._client = client
        self.row_factory = None  # aiosqlite-совместимость; libsql всегда даёт dict

    def execute(self, sql: str, params: tuple | list = ()) -> _ExecuteAwaitable:
        return _ExecuteAwaitable(self._client, sql, params)

    async def executescript(self, script: str) -> None:
        for statement in split_statements(script):
            await self._client.execute(statement)

    async def commit(self) -> None:
        # У Turso каждый execute() уже зафиксирован сам по себе — здесь
        # держим совместимость по вызову, а не по поведению.
        return None

    async def close(self) -> None:
        await self._client.close()


def connect(url: str, auth_token: str | None) -> RemoteConnection:
    import libsql_client

    # Turso сам выдаёт адрес как libsql://..., и эта схема у клиента
    # означает hrana поверх WebSocket. Против настоящего сервера Turso
    # рукопожатие по нему не проходит ("400 Invalid response status"),
    # а обычный HTTPS с тем же хостом отрабатывает без проблем — и нам
    # он и не хуже: соединение не держится между запросами, каждый
    # execute() самостоятелен, стриминг не нужен.
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://") :]

    client = libsql_client.create_client(url, auth_token=auth_token or None)
    return RemoteConnection(client)
