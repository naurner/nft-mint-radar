"""Обновление с GitHub по кнопке в самом боте.

Сам бот внутри контейнера не может выполнить git или Docker — у контейнера
нет ни .git, ни доступа к Docker (см. Dockerfile: только установленный
пакет). Поэтому кнопка не запускает команды сама, а оставляет метку на
общем томе data/ (см. docker-compose.yml — тот же путь смонтирован на
хосте как ./data), а фактическое обновление делает mac-autopull.sh —
launchd следит за этим файлом (WatchPaths) и запускает проверку сразу,
не дожидаясь планового пятиминутного цикла.

Три файла на этом томе:
- update_requested.json — пишет бот, когда нажали кнопку.
- update_inflight.json — хост переименовывает запрос в него, взявшись за
  дело; так бот, даже перезапустившись посреди обновления (а он
  перезапустится — весь смысл в пересборке контейнера), знает, куда
  прислать результат.
- update_result.txt — пишет хост, закончив; бот читает и удаляет вместе
  с update_inflight.json.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class HostUpdater:
    def __init__(self, data_dir: Path):
        self._request = data_dir / "update_requested.json"
        self._inflight = data_dir / "update_inflight.json"
        self._result = data_dir / "update_result.txt"

    def has_pending(self) -> bool:
        return self._request.exists() or self._inflight.exists()

    def request(self, *, chat_id: int, message_id: int) -> bool:
        """False — обновление уже запрошено и ещё не завершилось."""
        if self.has_pending():
            return False
        self._request.parent.mkdir(parents=True, exist_ok=True)
        self._request.write_text(
            f'{{"chat_id": {chat_id}, "message_id": {message_id}}}', encoding="utf-8"
        )
        return True

    def poll_result(self) -> dict | None:
        """Забирает и удаляет результат, если хост уже закончил, иначе None.

        Объединяет статус из update_result.txt с chat_id/message_id из
        update_inflight.json — они в разных файлах, потому что их пишут
        разные процессы (бот и хост-скрипт) в разное время.
        """
        if not self._result.exists():
            return None

        result: dict = {}
        try:
            for line in self._result.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    result[key.strip()] = value.strip()
        except OSError:
            log.exception("не удалось прочитать результат обновления")

        if self._inflight.exists():
            try:
                import json

                result.update(json.loads(self._inflight.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.exception("не удалось прочитать chat_id/message_id обновления")

        self._result.unlink(missing_ok=True)
        self._inflight.unlink(missing_ok=True)
        return result or None


def format_result(result: dict) -> str:
    status = result.get("status")
    frm = result.get("from", "")
    to = result.get("to", "")
    detail = html.escape(result.get("detail", ""))
    span = f"{frm} → {to}" if frm and to else to or frm

    if status == "ok":
        return f"✅ <b>Обновлено</b>: {span}\n{detail}"
    if status == "unchanged":
        return f"ℹ️ Уже на последнем коммите ({span})\n{detail}"
    return f"❌ <b>Не получилось обновиться</b>{f' ({span})' if span else ''}\n{detail}"
