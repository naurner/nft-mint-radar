from datetime import datetime, timezone

from wlparser.enrich.models import ProjectLinks
from wlparser.models import Signal
from wlparser.notify.telegram import (
    TelegramBroadcaster,
    format_alert,
    is_permanent_failure,
)

NFT = "0x" + "a" * 40
OWNER = 111
GUEST = 222

FULL_LINKS = ProjectLinks(
    explorer=f"https://robinscan.io/token/{NFT}",
    opensea="https://opensea.io/collection/clunker-hood",
    website="https://www.clunkerhood.xyz/",
    twitter="https://x.com/Clunkerhood",
    discord="https://discord.gg/abc",
    collection_name="Clunkerhood",
    description="6,399 beaters — the cars nobody would drive.",
)


def signal(kind="contract_deployed", **kwargs):
    base = dict(
        source="onchain",
        kind=kind,
        text="Mint confirmed: Clunkerhood, 34 wallets",
        url=None,
        author=None,
        contract_address=NFT,
        chain="robinhood",
        timestamp=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        raw={},
    )
    return Signal(**{**base, **kwargs})


class FakeBot:
    def __init__(self, fail_for=None, error=None):
        self.sent = []
        self.next_id = 100
        self.fail_for = fail_for
        self.error = error or RuntimeError("boom")

    async def send_message(self, **kwargs):
        if self.fail_for is not None and kwargs["chat_id"] == self.fail_for:
            raise self.error
        self.sent.append(kwargs)
        self.next_id += 1
        return type("Msg", (), {"message_id": self.next_id})()


class FakeEnricher:
    def __init__(self, links=None):
        self.links = links or ProjectLinks()
        self.asked = []

    async def enrich(self, address):
        self.asked.append(address)
        return self.links


def make(bot, chat_ids, threads=None, **kwargs):
    threads = threads if threads is not None else {}
    deactivated = []

    async def recipients():
        return list(chat_ids)

    def lookup(chat_id, address):
        return threads.get((chat_id, address))

    async def store(chat_id, address, message_id):
        threads[(chat_id, address)] = message_id

    async def on_fail(chat_id):
        deactivated.append(chat_id)

    kwargs.setdefault("send_delay_s", 0)
    broadcaster = TelegramBroadcaster(
        bot,
        recipients=recipients,
        thread_lookup=lookup,
        thread_store=store,
        on_permanent_failure=on_fail,
        owner_chat_id=OWNER,
        **kwargs,
    )
    return broadcaster, threads, deactivated


# --- формат сообщения ---


def test_explorer_link_points_at_token_page():
    assert f'href="https://robinscan.io/token/{NFT}"' in format_alert(
        signal(), FULL_LINKS
    )


def test_all_socials_are_rendered():
    text = format_alert(signal(), FULL_LINKS)
    assert "https://opensea.io/collection/clunker-hood" in text
    assert "https://x.com/Clunkerhood" in text
    assert "https://discord.gg/abc" in text
    assert "https://www.clunkerhood.xyz/" in text


def test_missing_links_are_simply_absent():
    text = format_alert(signal(), ProjectLinks(explorer="https://robinscan.io/token/x"))
    assert "OpenSea" not in text
    assert "robinscan.io/token/x" in text


def test_contract_address_is_shown_as_copyable_code():
    assert f"<code>{NFT}</code>" in format_alert(signal(), FULL_LINKS)


def test_description_is_truncated_and_escaped():
    text = format_alert(
        signal(), ProjectLinks(description="<script>x</script>" + "ф" * 400)
    )
    assert "…" in text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_signal_text_is_escaped():
    assert "&lt;b&gt;" in format_alert(signal(text="<b>x</b>"), ProjectLinks())


def test_works_without_links_at_all():
    assert "Начался минт" in format_alert(signal(kind="mint_started"))


# --- рассылка ---


async def test_alert_reaches_every_subscriber():
    bot = FakeBot()
    broadcaster, _, _ = make(bot, [OWNER, GUEST])
    await broadcaster.send(signal())
    assert {msg["chat_id"] for msg in bot.sent} == {OWNER, GUEST}


async def test_owner_receives_the_alert_first():
    bot = FakeBot()
    broadcaster, _, _ = make(bot, [GUEST, OWNER])
    await broadcaster.send(signal())
    assert bot.sent[0]["chat_id"] == OWNER


async def test_each_chat_gets_its_own_thread():
    # message_id из чата владельца нельзя использовать как reply в чужом чате.
    bot = FakeBot()
    broadcaster, threads, _ = make(bot, [OWNER, GUEST])
    await broadcaster.send(signal())
    assert threads[(OWNER, NFT)] != threads[(GUEST, NFT)]


async def test_second_signal_replies_into_the_right_thread():
    bot = FakeBot()
    threads = {(OWNER, NFT): 55, (GUEST, NFT): 77}
    broadcaster, _, _ = make(bot, [OWNER, GUEST], threads)
    await broadcaster.send(signal(kind="mint_started"))
    by_chat = {msg["chat_id"]: msg["reply_to_message_id"] for msg in bot.sent}
    assert by_chat == {OWNER: 55, GUEST: 77}


async def test_blocked_user_is_deactivated():
    bot = FakeBot(fail_for=GUEST, error=RuntimeError("Forbidden: bot was blocked"))
    broadcaster, _, deactivated = make(bot, [OWNER, GUEST])
    await broadcaster.send(signal())
    assert deactivated == [GUEST]
    assert bot.sent[0]["chat_id"] == OWNER


async def test_one_failing_chat_does_not_stop_the_rest():
    bot = FakeBot(fail_for=OWNER, error=RuntimeError("some transient error"))
    broadcaster, _, _ = make(bot, [OWNER, GUEST])
    await broadcaster.send(signal())
    assert [msg["chat_id"] for msg in bot.sent] == [GUEST]


async def test_deleted_thread_message_falls_back_to_a_plain_send():
    class Picky(FakeBot):
        async def send_message(self, **kwargs):
            if "reply_to_message_id" in kwargs:
                raise RuntimeError("Bad Request: message to be replied not found")
            return await super().send_message(**kwargs)

    bot = Picky()
    broadcaster, _, _ = make(bot, [OWNER], {(OWNER, NFT): 55})
    await broadcaster.send(signal(kind="mint_started"))
    assert len(bot.sent) == 1
    assert "reply_to_message_id" not in bot.sent[0]


async def test_rate_limit_is_retried_after_waiting():
    class Limited(FakeBot):
        def __init__(self):
            super().__init__()
            self.first = True

        async def send_message(self, **kwargs):
            if self.first:
                self.first = False
                exc = RuntimeError("Too Many Requests")
                exc.retry_after = 0
                raise exc
            return await super().send_message(**kwargs)

    bot = Limited()
    broadcaster, _, _ = make(bot, [OWNER])
    await broadcaster.send(signal())
    assert len(bot.sent) == 1


async def test_enricher_is_called_once_per_alert_not_per_chat():
    bot = FakeBot()
    enricher = FakeEnricher(FULL_LINKS)
    broadcaster, _, _ = make(bot, [OWNER, GUEST], enricher=enricher)
    await broadcaster.send(signal())
    assert enricher.asked == [NFT]
    assert all("robinscan.io" in msg["text"] for msg in bot.sent)


async def test_no_subscribers_is_handled_quietly():
    bot = FakeBot()
    broadcaster, _, _ = make(bot, [])
    await broadcaster.send(signal())
    assert bot.sent == []


def test_permanent_failure_detection():
    assert is_permanent_failure(RuntimeError("Forbidden: bot was blocked by the user"))
    assert is_permanent_failure(RuntimeError("Bad Request: chat not found"))
    assert not is_permanent_failure(RuntimeError("Internal Server Error"))


# --- перевод цены в доллары ---


def test_usd_price_line_is_shown_when_provided():
    text = format_alert(signal(), FULL_LINKS, usd_price="$34.50 с газом")
    assert "$34.50 с газом" in text
    assert "💵" in text


def test_usd_price_line_is_absent_by_default():
    text = format_alert(signal(), FULL_LINKS)
    assert "💵" not in text


def test_usd_price_is_escaped():
    text = format_alert(signal(), FULL_LINKS, usd_price="<b>$1</b>")
    assert "<b>$1</b>" not in text
    assert "&lt;b&gt;" in text


async def test_pricing_is_consulted_only_for_onchain_signals_with_price():
    class FakePricing:
        def __init__(self, line):
            self.line = line
            self.asked = []

        async def usd_line(self, mint_price_wei):
            self.asked.append(mint_price_wei)
            return self.line

    bot = FakeBot()
    pricing = FakePricing("$12.00")
    sig = signal()
    sig.raw["info"] = {"mint_price_wei": 10**16}
    broadcaster, _, _ = make(bot, [OWNER], pricing=pricing)
    await broadcaster.send(sig)
    assert pricing.asked == [10**16]
    assert "$12.00" in bot.sent[0]["text"]


async def test_pricing_is_skipped_without_a_known_price():
    class FailIfCalled:
        async def usd_line(self, mint_price_wei):
            raise AssertionError("не должен вызываться без цены")

    bot = FakeBot()
    broadcaster, _, _ = make(bot, [OWNER], pricing=FailIfCalled())
    await broadcaster.send(signal())  # raw пуст, цены нет
    assert "💵" not in bot.sent[0]["text"]
