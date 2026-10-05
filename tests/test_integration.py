"""Сквозной прогон: логи сети -> сигнал -> база -> сообщения подписчикам.

Юнит-тесты проверяют детали по отдельности; здесь проверяется, что
компоненты действительно соединены друг с другом правильно.
"""

import asyncio

from wlparser.chain.selectors import (
    SEL_NAME,
    SEL_SUPPORTS_INTERFACE,
    SEL_SYMBOL,
    TRANSFER_TOPIC,
    ZERO_TOPIC,
)
from wlparser.db import Database
from wlparser.enrich.models import ProjectLinks
from wlparser.enrich.service import Enricher
from wlparser.notify.commands import SubscriptionService
from wlparser.notify.telegram import TelegramBroadcaster
from wlparser.pipeline.runner import Pipeline
from wlparser.sources.onchain import OnchainSource

OWNER = 111
GUEST = 222
NFT = "0x" + "a" * 40
NOISE = "0x" + "b" * 40
WORD_TRUE = "0x" + "0" * 63 + "1"


def string_word(text: str) -> str:
    word = lambda n: format(n, "064x")  # noqa: E731
    return "0x" + word(32) + word(len(text)) + text.encode().hex().ljust(64, "0")


def mint(address: str, to: int):
    return {
        "address": address,
        "topics": [
            TRANSFER_TOPIC,
            ZERO_TOPIC,
            "0x" + "0" * 24 + format(to, "040x"),
            "0x" + format(to, "064x"),
        ],
    }


class FakeChain:
    """Настоящая коллекция минтится рядом со служебной NFT."""

    NAMES = {NFT: "Onchain Harvest", NOISE: "Uniswap v4 Positions NFT"}

    def __init__(self):
        self.logs = [mint(NFT, i) for i in range(1, 6)]
        self.logs += [mint(NOISE, 100 + i) for i in range(1, 20)]

    async def call(self, method, params=None):
        if method == "eth_blockNumber":
            return hex(10_000)
        raise AssertionError(method)

    async def batch(self, calls):
        if calls and calls[0][0] == "eth_getLogs":
            return [self.logs, []]
        out = []
        for _method, params in calls:
            name = self.NAMES.get(params[0]["to"].lower())
            data = params[0]["data"]
            if name is None:
                out.append(None)
            elif data.startswith(SEL_SUPPORTS_INTERFACE) and "80ac58cd" in data:
                out.append(WORD_TRUE)
            elif data.startswith(SEL_NAME):
                out.append(string_word(name))
            elif data.startswith(SEL_SYMBOL):
                out.append(string_word(name[:4].upper()))
            else:
                out.append(None)
        return out


class FakeOpenSea:
    def __init__(self, links):
        self.links = links
        self.calls = 0

    async def lookup(self, address):
        self.calls += 1
        return self.links


class FakeBot:
    def __init__(self):
        self.sent = []
        self.next_id = 500

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self.next_id += 1
        return type("Msg", (), {"message_id": self.next_id})()


async def test_mint_reaches_every_subscriber_with_links_and_no_noise(tmp_path):
    db = Database(tmp_path / "e2e.sqlite")
    await db.connect()

    bot = FakeBot()
    threads: dict[tuple[int, str], int] = {}
    owner_notes: list[str] = []

    async def store(chat_id, address, message_id):
        threads[(chat_id, address)] = message_id
        await db.set_thread_id(chat_id, address, message_id)

    async def tell_owner(text):
        owner_notes.append(text)

    service = SubscriptionService(db, owner_chat_id=OWNER, notify_owner=tell_owner)
    await service.ensure_owner()
    await service.subscribe(GUEST, username="alice", display_name="Alice")

    opensea = FakeOpenSea(
        ProjectLinks(
            opensea="https://opensea.io/collection/onchain-harvest",
            twitter="https://x.com/OnchainHarvest",
        )
    )
    notifier = TelegramBroadcaster(
        bot,
        recipients=service.active_chat_ids,
        thread_lookup=lambda chat_id, address: threads.get((chat_id, address)),
        thread_store=store,
        on_permanent_failure=service.deactivate,
        owner_chat_id=OWNER,
        send_delay_s=0,
        enricher=Enricher(
            opensea, explorer_url="https://robinscan.io", store=db.save_links
        ),
    )

    source = OnchainSource(
        FakeChain(), chain_name="robinhood", start_block=100, min_unique_minters=5
    )
    # mint_started сам по себе больше не мгновенный алерт по умолчанию (см.
    # Pipeline.URGENT_KINDS — только всплеск/почти распродано, остальное
    # уходит в периодический дайджест) — этому e2e-тесту нужен именно
    # мгновенный путь, чтобы проверить доставку/обогащение/треды целиком.
    pipeline = Pipeline(db, notifier, urgent_kinds=frozenset({"mint_started"}))
    queue: asyncio.Queue = asyncio.Queue()

    worker = asyncio.create_task(pipeline.run(queue))
    await source.process_range(100, 600, queue)
    await queue.join()
    worker.cancel()

    # Одно событие на двух подписчиков. Служебная NFT, которой в диапазоне
    # было 19 минтов, не попала никуда.
    assert len(bot.sent) == 2
    assert {msg["chat_id"] for msg in bot.sent} == {OWNER, GUEST}
    assert bot.sent[0]["chat_id"] == OWNER  # владелец получает первым
    assert all("Uniswap" not in msg["text"] for msg in bot.sent)

    for message in bot.sent:
        assert "5 wallets" in message["text"]
        assert f"https://robinscan.io/token/{NFT}" in message["text"]
        assert "https://opensea.io/collection/onchain-harvest" in message["text"]
        assert "https://x.com/OnchainHarvest" in message["text"]

    # OpenSea опрошен один раз на контракт, а не на каждого подписчика.
    assert opensea.calls == 1

    # Владельцу пришло уведомление о новом подписчике.
    assert owner_notes and "Alice" in owner_notes[0]

    project = await db.get_project(NFT)
    assert project["standard"] == "erc721"
    assert project["twitter"] == "https://x.com/OnchainHarvest"
    assert set(await db.load_threads()) == {(OWNER, NFT), (GUEST, NFT)}
    assert {row["address"] for row in await db.recent_signals()} == {NFT}

    await db.close()


async def test_restart_resumes_from_saved_cursor(tmp_path):
    # Перезапуск не должен терять блоки и не должен начинать с головы цепи.
    db = Database(tmp_path / "resume.sqlite")
    await db.connect()
    await db.set_cursor("block", "104")

    source = OnchainSource(
        FakeChain(),
        chain_name="robinhood",
        start_block=int(await db.get_cursor("block")),
    )
    assert source.next_block == 104
    await db.close()
