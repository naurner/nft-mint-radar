"""Командная строка wlparser."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table


def _force_utf8_output() -> None:
    """Консоль Windows по умолчанию не в UTF-8, и кириллица роняет вывод.

    Переключаем потоки до того, как typer или rich что-либо напечатают.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_force_utf8_output()

from .app import BotAlreadyRunningError, run_app

# Отдельный код выхода: батник по нему понимает, что перезапускать
# бесполезно — сначала нужно остановить второй экземпляр.
EXIT_ALREADY_RUNNING = 3
from .chain.probe import probe_endpoint
from .chain.rpc import JsonRpcClient, http_transport
from .config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH, load_settings
from .db import Database
from .setup_wizard import write_config, write_env

app = typer.Typer(
    help="Парсер новых WL-раздач и минтов в сети Robinhood Chain",
    no_args_is_help=True,
)
console = Console()


@app.command("chain-probe")
def chain_probe(rpc_url: str) -> None:
    """Проверить, годится ли RPC-адрес для работы бота."""

    async def main() -> None:
        client = JsonRpcClient(http_transport(rpc_url))
        try:
            result = await probe_endpoint(client)
        finally:
            await client.aclose()

        if result.usable:
            console.print(
                f"[green]Работает.[/] chain id {result.chain_id}, "
                f"высота блока {result.block_number}"
            )
        else:
            console.print(f"[red]Не годится:[/] {result.problem}")
            raise typer.Exit(code=1)

    asyncio.run(main())


@app.command("db-probe")
def db_probe(url: str, auth_token: str = "") -> None:
    """Проверить подключение к общей базе (Turso) до того, как класть в конфиг.

    Токен можно не передавать аргументом — тогда берётся из .env
    (DB_AUTH_TOKEN), чтобы не светить секрет в истории команд оболочки.
    """

    async def main() -> None:
        token = auth_token or load_settings().db_auth_token
        db = Database(url, auth_token=token or None)
        try:
            await db.connect()
            await db.get_cursor("_probe")  # любой лёгкий запрос к схеме
        except Exception as exc:
            console.print(f"[red]Не подключился:[/] {exc}")
            raise typer.Exit(code=1)
        else:
            console.print("[green]Подключение работает.[/] Схема создана/на месте.")
        finally:
            await db.close()

    asyncio.run(main())


@app.command()
def setup(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Первичная настройка: RPC-адрес сети и Telegram."""

    async def main() -> None:
        console.print("[bold]Настройка WL-парсера[/]\n")
        console.print("[bold]Шаг 1 из 2.[/] Адрес RPC сети Robinhood Chain.")
        console.print(
            "Найти можно на chainlist.org или в документации сети. "
            "Проверю сразу, годится ли он.\n"
        )

        while True:
            rpc_url = typer.prompt("RPC URL").strip()
            client = JsonRpcClient(http_transport(rpc_url))
            console.print("Проверяю...")
            try:
                result = await probe_endpoint(client)
            finally:
                await client.aclose()

            if result.usable:
                console.print(
                    f"[green]Годится.[/] chain id {result.chain_id}, "
                    f"высота блока {result.block_number}\n"
                )
                break
            console.print(f"[red]Не подходит:[/] {result.problem}\n")

        explorer_url = typer.prompt(
            "Адрес эксплорера для ссылок в алертах (можно оставить пустым)",
            default="",
            show_default=False,
        ).strip()

        console.print("\n[bold]Шаг 2 из 2.[/] Telegram.")
        console.print("Открой в Telegram бота @BotFather, отправь /newbot,")
        console.print("придумай имя — он выдаст токен вида 1234567:AAF...\n")
        token = typer.prompt("Токен бота").strip()

        console.print("\nТеперь напиши своему новому боту любое сообщение,")
        console.print("затем открой @userinfobot — он покажет твой числовой id.\n")
        chat_id = typer.prompt("Твой chat id").strip()

        write_config(
            config_path,
            rpc_url=rpc_url,
            chain_id=result.chain_id,
            explorer_url=explorer_url,
        )
        write_env(
            env_path,
            {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id},
        )

        console.print(
            f"\n[green]Готово.[/] Настройки записаны в {config_path} и {env_path}\n"
            "Запускай: [bold]python -m wlparser run[/]"
        )

    asyncio.run(main())


@app.command()
def run(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Запустить наблюдение за сетью."""
    settings = load_settings(config_path)

    # Вдобавок к stdout (его читает `docker compose logs` снаружи контейнера)
    # — файл в томе data/, чтобы бот мог прочитать собственный хвост лога
    # изнутри и переслать в Telegram командой /logs, без терминала на Маке.
    from logging.handlers import RotatingFileHandler

    log_path = Path(settings.db_path).parent / "bot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=2, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )
    # Библиотеки логируют каждый HTTP-запрос: при десяти запросах в секунду
    # это забивает лог так, что собственных сообщений бота в нём не видно.
    for noisy in ("httpx", "httpcore", "aiogram.event", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not settings.is_configured:
        console.print(
            "[red]Не настроено.[/] Сначала запусти: [bold]python -m wlparser setup[/]"
        )
        raise typer.Exit(code=1)
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        console.print(
            "[red]Нет данных Telegram.[/] Запусти: [bold]python -m wlparser setup[/]"
        )
        raise typer.Exit(code=1)

    console.print("[green]Слежу за сетью.[/] Остановить — Ctrl+C\n")
    try:
        asyncio.run(run_app(settings))
    except KeyboardInterrupt:
        console.print("\nОстановлено. Прогресс сохранён.")


@app.command()
def subscribers(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Показать, кто подписан на бота."""

    async def main() -> None:
        settings = load_settings(config_path)
        db = Database.from_settings(settings)
        await db.connect()
        try:
            rows = await db.list_subscribers(active_only=False)
        finally:
            await db.close()

        if not rows:
            console.print("Подписчиков пока нет.")
            return

        table = Table(title=f"Подписчики ({sum(r['active'] for r in rows)} активных)")
        table.add_column("chat id", style="dim")
        table.add_column("Имя")
        table.add_column("Юзернейм")
        table.add_column("Пришёл")
        table.add_column("Статус")
        for row in rows:
            status = "владелец" if row["is_owner"] else ("активен" if row["active"] else "отписан")
            table.add_row(
                str(row["chat_id"]),
                row["display_name"] or "",
                f"@{row['username']}" if row["username"] else "",
                (row["joined_at"] or "")[:10],
                status,
            )
        console.print(table)

    asyncio.run(main())


@app.command("twitter-add")
def twitter_add(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Подключить аккаунт X для чтения ленты.

    Нужны два значения из cookies браузера, где ты залогинен в X.
    """

    async def main() -> None:
        from twscrape import API

        settings = load_settings(config_path)
        console.print("[bold]Подключение аккаунта X[/]\n")
        console.print(
            "Настоятельно советую использовать [bold]отдельный[/] аккаунт, "
            "а не основной:\nчтение через cookies нарушает правила X, "
            "и аккаунт могут заблокировать.\n"
        )
        console.print("Где взять значения — в браузере, где ты залогинен в x.com:")
        console.print("  F12 -> вкладка Application -> Cookies -> https://x.com")
        console.print("  Найди строки [bold]auth_token[/] и [bold]ct0[/].\n")

        username = typer.prompt("Имя аккаунта (без @, для подписи)").strip()
        auth_token = typer.prompt("auth_token").strip()
        ct0 = typer.prompt("ct0").strip()

        api = API(settings.twitter.accounts_db)
        await api.pool.add_account(
            username,
            "-",
            f"{username}@local",
            "-",
            cookies=f"auth_token={auth_token}; ct0={ct0}",
        )
        console.print(f"\n[green]Аккаунт {username} добавлен.[/]")
        console.print(
            "Проверь: [bold]python -m wlparser twitter-test[/]\n"
            "Включить источник: в config.yaml поставь [bold]twitter.enabled: true[/]"
        )

    asyncio.run(main())


@app.command("poster-enable")
def poster_enable(
    username: str = typer.Argument(
        "", help="Имя аккаунта из twitter-add; если он один — угадывается сам"
    ),
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    """Включить автопост в X (с подтверждением каждого поста в Telegram)."""

    async def main() -> None:
        from twscrape import AccountsPool

        from .setup_wizard import enable_poster

        settings = load_settings(config_path)
        accounts = await AccountsPool(settings.twitter.accounts_db).get_all()
        names = [a.username for a in accounts]

        chosen = username.strip()
        if not chosen:
            if len(names) == 1:
                chosen = names[0]
            elif not names:
                console.print(
                    "[red]Нет ни одного аккаунта X.[/] Сначала подключи его: "
                    "[bold]mac-twitter.command[/]"
                )
                raise typer.Exit(1)
            else:
                console.print(
                    "[yellow]Аккаунтов несколько:[/] "
                    + ", ".join(names)
                    + "\nУкажи нужный: [bold]poster-enable <имя>[/]"
                )
                raise typer.Exit(1)

        account = next((a for a in accounts if a.username == chosen), None)
        if account is None:
            console.print(
                f"[red]Аккаунта {chosen} нет.[/] Есть: {', '.join(names) or 'никого'}"
            )
            raise typer.Exit(1)
        if not account.has_session:
            console.print(
                f"[red]У {chosen} нет рабочей сессии (cookies).[/] "
                "Добавь аккаунт заново: [bold]mac-twitter.command[/]"
            )
            raise typer.Exit(1)

        enable_poster(config_path, chosen)
        console.print(f"[green]Автопост включён от имени {chosen}.[/]")
        console.print(
            "Бот только присылает черновики в Telegram — публикует лишь после "
            "твоего нажатия «Опубликовать».\nЧтобы настройка подхватилась, "
            "перезапусти бота: [bold]docker compose restart bot[/]"
        )

    asyncio.run(main())


@app.command("llm-model")
def llm_model(
    model: str = typer.Argument(..., help="Имя модели в Ollama, например qwen2.5:3b"),
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    """Задать модель Ollama для ИИ — без правки config.yaml руками."""
    from .setup_wizard import set_llm_model

    set_llm_model(config_path, model)
    console.print(f"[green]Модель ИИ установлена: {model}[/]")
    console.print(
        "Используется и для текста постов, и для отсева спама в алертах.\n"
        "Чтобы настройка подхватилась, перезапусти бота: "
        "[bold]docker compose restart bot[/]"
    )


@app.command("db-use-local")
def db_use_local(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Вернуться на локальный файл базы, перенеся туда всё из Turso, если получится.

    Например, если бесплатный лимит Turso кончился. Пробует прочитать
    текущую базу (settings.db_url) и скопировать всё в локальный файл;
    если Turso недоступен даже для чтения (лимит исчерпан жёстко) — просто
    переключается на пустую локальную базу, не роняя всю команду.
    """
    from .setup_wizard import use_local_db

    async def main() -> None:
        settings = load_settings(config_path)
        if settings.db_url:
            source = Database(settings.db_url, auth_token=settings.db_auth_token)
            destination = Database(settings.db_path)
            try:
                await source.connect()
            except Exception as exc:
                console.print(
                    f"[yellow]Turso недоступен для чтения ({exc.__class__.__name__}):"
                    f" {exc}[/]"
                )
                console.print("Перенести историю не получится — переключаюсь на пустую базу.")
                try:
                    await source.close()
                except Exception:
                    pass  # соединение и не открылось толком — закрывать нечего
            else:
                await destination.connect()
                try:
                    counts = await source.copy_all_rows_to(destination)
                finally:
                    await source.close()
                    await destination.close()
                console.print("[green]Перенесено из Turso:[/]")
                for table, n in counts.items():
                    if n:
                        console.print(f"  {table}: {n}")
        else:
            console.print("[yellow]db_url не задан — база уже локальная.[/]")

    asyncio.run(main())

    use_local_db(config_path)
    console.print("[green]Конфиг переключён на локальный файл.[/]")
    console.print(
        "Чтобы настройка подхватилась, перезапусти бота: "
        "[bold]docker compose restart bot[/]"
    )


@app.command("posts-clear-pending")
def posts_clear_pending(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Списать все висящие черновики постов в X (публикует их не пытается)."""

    async def main() -> None:
        settings = load_settings(config_path)
        db = Database.from_settings(settings)
        await db.connect()
        try:
            n = await db.clear_pending_posts()
        finally:
            await db.close()

        if n == 0:
            console.print("Черновиков в очереди не было.")
        else:
            console.print(f"[green]Списано черновиков: {n}[/]")
            console.print(
                "Старые кнопки под ними в Telegram теперь просто ответят,"
                " что черновик не актуален."
            )

    asyncio.run(main())


@app.command("poster-test-draft")
def poster_test_draft(
    text: str = typer.Argument(
        "", help="Текст черновика для проверки; пусто — сгенерировать с меткой времени"
    ),
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    """Прислать тестовый черновик в Telegram, минуя дневной лимит предложений.

    Для проверки, что публикация в X реально работает, когда обычных
    предложений сегодня уже не будет — например, дневной лимит съели старые
    черновики, которые не удалось опубликовать до фикса. Публикует бот, уже
    запущенный в Docker: эта команда только создаёт черновик и присылает те
    же кнопки — сам твит уходит после нажатия «Опубликовать», как обычно.
    """
    import html as html_mod
    from datetime import datetime, timezone

    from aiogram import Bot

    from .notify.keyboards import post_draft_keyboard

    async def main() -> None:
        settings = load_settings(config_path)
        if not settings.telegram_chat_id:
            console.print("[red]Не задан telegram_chat_id в конфиге.[/]")
            raise typer.Exit(1)

        # X отклоняет повторную публикацию буквально того же текста как
        # дубликат — со статичным дефолтом вторая проверка подряд падала бы
        # именно на этом, а не на том, что реально нужно проверить.
        draft_text = text.strip() or (
            "Тестовый пост — проверяем публикацию в X "
            f"({datetime.now(timezone.utc):%H:%M:%S} UTC)."
        )

        db = Database.from_settings(settings)
        await db.connect()
        try:
            post_id = await db.create_pending_post(address=None, text=draft_text)
        finally:
            await db.close()

        bot = Bot(token=settings.telegram_bot_token)
        try:
            await bot.send_message(
                chat_id=int(settings.telegram_chat_id),
                text=f"🐦 <b>Тестовый черновик поста в X</b>\n\n{html_mod.escape(draft_text)}",
                parse_mode="HTML",
                reply_markup=post_draft_keyboard(post_id),
            )
        finally:
            await bot.session.close()

        console.print(f"[green]Черновик #{post_id} отправлен в Telegram.[/]")
        console.print("Нажми там «Опубликовать» и посмотри, что ответит бот.")

    asyncio.run(main())


@app.command("twitter-test")
def twitter_test(
    query: str = "", limit: int = 5, config_path: Path = DEFAULT_CONFIG_PATH
) -> None:
    """Проверить, что чтение X работает: один поиск и разбор результата."""

    async def main() -> None:
        from twscrape import API

        from .sources.extract import classify, extract_addresses

        settings = load_settings(config_path)
        search = query or (
            settings.twitter.queries[0] if settings.twitter.queries else "nft mint"
        )
        console.print(f"Запрос: [bold]{search}[/]\n")

        api = API(settings.twitter.accounts_db)
        found = 0
        async for tweet in api.search(search, limit=limit):
            found += 1
            kind = classify(tweet.rawContent or "")
            addresses = extract_addresses(tweet.rawContent or "")
            colour = "green" if kind == "wl_announced" else "dim"
            console.print(f"[{colour}]{kind}[/] @{tweet.user.username}: "
                          f"{(tweet.rawContent or '')[:100]}")
            if addresses:
                console.print(f"    контракт: {addresses[0]}")

        if found:
            console.print(f"\n[green]Работает.[/] Получено твитов: {found}")
        else:
            console.print(
                "\n[yellow]Ничего не найдено.[/] Либо по запросу пусто, "
                "либо аккаунт не принят —\nдобавь его заново: "
                "[bold]python -m wlparser twitter-add[/]"
            )

    asyncio.run(main())


@app.command()
def recent(limit: int = 20, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Показать последние находки, включая те, что не прошли порог алерта."""

    async def main() -> None:
        settings = load_settings(config_path)
        db = Database.from_settings(settings)
        await db.connect()
        try:
            rows = await db.recent_signals(limit)
        finally:
            await db.close()

        if not rows:
            console.print("Пока ничего не найдено.")
            return

        table = Table(title="Последние сигналы")
        table.add_column("Время", style="dim")
        table.add_column("Тип")
        table.add_column("Оценка", justify="right")
        table.add_column("Описание")
        for row in rows:
            table.add_row(
                (row["ts"] or "")[:16].replace("T", " "),
                row["kind"],
                str(row["score"] if row["score"] is not None else ""),
                (row["text"] or "")[:70],
            )
        console.print(table)

    asyncio.run(main())


@app.command()
def top(limit: int = 10, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Отслеживаемые проекты по эвристической оценке интереса.

    Не финансовый совет — считается по скорости набора держателей и
    отношению флора к цене минта. Пока данных мало, оценки грубые.
    """

    async def main() -> None:
        settings = load_settings(config_path)
        db = Database.from_settings(settings)
        await db.connect()
        try:
            rows = await db.top_projects(limit)
        finally:
            await db.close()

        if not rows:
            console.print(
                "Пока нечего показать. Оценка появляется через несколько "
                "проверок после первого алерта по проекту."
            )
            return

        table = Table(title="Что выглядит интереснее (не совет)")
        table.add_column("Балл", justify="right")
        table.add_column("Проект")
        table.add_column("Почему")
        table.add_column("Адрес", style="dim")
        for row in rows:
            table.add_row(
                str(row["interest_score"]),
                row["name"] or row["symbol"] or "",
                row["interest_reason"] or "",
                row["address"],
            )
        console.print(table)

    asyncio.run(main())


@app.command()
def find(query: str, limit: int = 3, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Найти уже отслеживаемый проект по названию и показать всё, что о нём известно."""

    async def main() -> None:
        import re

        import httpx

        from .app import _build_opensea
        from .enrich.website_price import WebsitePriceLookup
        from .tracking.service import LiveTracker

        settings = load_settings(config_path)
        db = Database.from_settings(settings)
        await db.connect()

        rows = await db.search_projects(query, limit=limit)
        if not rows:
            console.print(f"Ничего не нашёл по «{query}».")
            await db.close()
            return

        client = JsonRpcClient(
            http_transport(settings.chain.endpoints),
            min_request_interval_s=settings.chain.min_request_interval_s,
        )
        http = httpx.AsyncClient(timeout=settings.links.timeout_s)

        async def _website_fetch(url: str):
            response = await http.get(url, follow_redirects=True)
            return response.status_code, response.text

        tracker = LiveTracker(
            db,
            client,
            editor=None,
            opensea=_build_opensea(settings, http),
            explorer_url=settings.chain.explorer_url,
            explorer_token_path=settings.chain.explorer_token_path,
            marketplace_chain=settings.links.opensea_chain,
            website=WebsitePriceLookup(_website_fetch)
            if settings.tracking.website_price_fallback_enabled
            else None,
            website_price_cache_ttl_s=settings.tracking.website_price_cache_ttl_s,
        )

        try:
            for row in rows:
                text = await tracker.render_card(row, recent_sales=True)
                console.print(re.sub(r"<[^>]+>", "", text))
                console.print("---")
        finally:
            await client.aclose()
            await http.aclose()
            await db.close()

    asyncio.run(main())


if __name__ == "__main__":
    app()
