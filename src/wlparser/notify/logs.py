"""Чтение собственного лог-файла бота — чтобы получить что-то вроде
`docker compose logs | grep ...` прямо в Telegram, без терминала на Маке.

`docker compose logs` читает stdout снаружи контейнера — бот туда достать
не может (нет сокета Docker, см. host_update.py). Поэтому логи вдобавок
пишутся в файл в томе data/ (см. cli.py::run) — его сам процесс читает
без проблем.
"""

from __future__ import annotations

from pathlib import Path


def filter_log(text: str, *, search: str | None, max_lines: int, context: int) -> str:
    """Без search — просто хвост. С search — совпавшие строки и `context`

    строк после каждой (как `grep -A N`), потому что сам факт ошибки редко
    интересен без traceback'а под ней.
    """
    lines = text.splitlines()
    if not search:
        return "\n".join(lines[-max_lines:])

    needle = search.lower()
    matched: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if needle in lines[i].lower():
            block = lines[i : i + context + 1]
            matched.extend(block)
            matched.append("---")
            i += len(block)
        else:
            i += 1

    return "\n".join(matched[-max_lines:]) if matched else ""


def read_log(
    path: str | Path,
    *,
    search: str | None = None,
    max_lines: int = 200,
    context: int = 40,
) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return filter_log(text, search=search, max_lines=max_lines, context=context)
