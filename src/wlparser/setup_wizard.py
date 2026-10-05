"""Мастер первичной настройки: пишет config.yaml и .env."""

from __future__ import annotations

from pathlib import Path

import yaml


def write_config(
    path: str | Path, *, rpc_url: str, chain_id: int | None, explorer_url: str
) -> None:
    """Обновляет секцию chain, не трогая остальные настройки.

    Пользователь мог поменять пороги вручную — перезапуск мастера
    не должен их сбрасывать.
    """
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    chain = data.setdefault("chain", {})
    chain["name"] = chain.get("name", "robinhood")
    chain["rpc_url"] = rpc_url
    chain["chain_id"] = chain_id
    chain["explorer_url"] = explorer_url
    chain.setdefault("poll_interval_s", 2.0)
    chain.setdefault("surge_threshold", 25)
    chain.setdefault("confirmations", 2)
    data.setdefault("alerts", {"min_score": 45})
    data.setdefault("db_path", "data/wlparser.sqlite")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def enable_poster(path: str | Path, username: str) -> None:
    """Включает автопост от имени аккаунта `username`, не трогая остальное.

    Те же правила, что у write_config: настройки, которые владелец мог
    поменять руками, не сбрасываются.
    """
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    data.setdefault("poster", {})["enabled"] = True
    data.setdefault("twitter", {})["post_as"] = username

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def set_llm_model(path: str | Path, model: str) -> None:
    """Закрепляет модель Ollama и включает ИИ, не трогая остальное.

    Владелец не редактирует config.yaml сам — эта функция стоит за
    CLI-командой `llm-model`, чтобы менять модель можно было одной строкой
    в терминале, а не поиском и правкой файла на Маке.
    """
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    llm = data.setdefault("llm", {})
    llm["enabled"] = True
    llm["model"] = model

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def use_local_db(path: str | Path) -> None:
    """Возвращает базу на локальный файл — убирает db_url, не трогая остальное.

    Для случая, когда общая база (Turso) стала недоступна — например,
    кончился бесплатный лимит, а платить владелец не готов. Database.
    from_settings сама переключается на локальный файл (db_path), если
    db_url пуст — тот же путь, что работал до подключения Turso. История,
    накопленная в Turso, сюда не переносится: начинается новая локальная
    база с нуля.
    """
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    data["db_url"] = ""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def write_env(path: str | Path, values: dict[str, str]) -> None:
    """Добавляет или обновляет ключи, сохраняя остальные строки файла."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out: list[str] = []

    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
