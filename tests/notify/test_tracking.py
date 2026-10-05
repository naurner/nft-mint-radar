from dataclasses import dataclass
from datetime import datetime, timezone

from wlparser.enrich.models import ProjectLinks
from wlparser.notify.tracking import LiveCardEditor, format_tracking_card

NFT = "0x" + "a" * 40
NOW = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)


@dataclass
class FakeInfo:
    total_supply: int | None
    max_supply: int | None = None
    mint_price_wei: int | None = None


PROJECT = {"address": NFT, "name": "Cash Bears", "symbol": "CB"}


def test_shows_progress_and_price():
    text = format_tracking_card(
        PROJECT,
        FakeInfo(total_supply=40, max_supply=100, mint_price_wei=10**16),
        ProjectLinks(),
        floor=None,
        num_owners=None,
        now=NOW,
        sold_out=False,
    )
    assert "40 / 100 (40%)" in text
    assert "0.01 ETH" in text
    assert "Cash Bears" in text


def test_free_mint_is_labeled():
    text = format_tracking_card(
        PROJECT,
        FakeInfo(total_supply=5, max_supply=100, mint_price_wei=0),
        ProjectLinks(),
        floor=None,
        num_owners=None,
        now=NOW,
        sold_out=False,
    )
    assert "бесплатно" in text


def test_sold_out_shows_red_marker():
    text = format_tracking_card(
        PROJECT,
        FakeInfo(total_supply=100, max_supply=100),
        ProjectLinks(),
        floor=None,
        num_owners=None,
        now=NOW,
        sold_out=True,
    )
    assert "🔴" in text
    assert "Распродано" in text


def test_active_uses_green_marker():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=10), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "🟢" in text


def test_floor_and_owners_are_shown_when_available():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=10, max_supply=100), ProjectLinks(),
        floor=0.25, num_owners=87, now=NOW, sold_out=False,
    )
    assert "0.25 ETH" in text
    assert "87" in text


def test_floor_shows_usd_estimate_when_given():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=10, max_supply=100), ProjectLinks(),
        floor=0.25, floor_usd="$500.00", num_owners=87, now=NOW, sold_out=False,
    )
    assert "0.25 ETH (≈ $500.00)" in text


def test_missing_floor_is_simply_absent():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=10, max_supply=100), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "флор" not in text


def test_falls_back_to_symbol_without_a_name():
    text = format_tracking_card(
        {"address": NFT, "symbol": "CB"}, FakeInfo(total_supply=1), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "CB" in text


def test_name_from_a_hostile_project_row_is_escaped():
    text = format_tracking_card(
        {"address": NFT, "name": "<script>x</script>"}, FakeInfo(total_supply=1),
        ProjectLinks(), floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_links_are_rendered_same_way_as_alerts():
    links = ProjectLinks(explorer="https://scan/token/0x1", opensea="https://opensea.io/x")
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1), links,
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "https://scan/token/0x1" in text
    assert "https://opensea.io/x" in text


def test_timestamp_is_shown():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "12:30" in text


# --- LiveCardEditor ---


class FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def edit_message_text(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


async def test_successful_edit_calls_the_bot_once():
    bot = FakeBot()
    await LiveCardEditor(bot).edit(111, 500, NFT, "text")
    assert len(bot.calls) == 1
    assert bot.calls[0]["chat_id"] == 111
    assert bot.calls[0]["message_id"] == 500


async def test_not_modified_is_treated_as_success():
    bot = FakeBot(error=RuntimeError("Bad Request: message is not modified"))
    await LiveCardEditor(bot).edit(111, 500, NFT, "text")  # не должно бросить


async def test_deleted_message_triggers_the_gone_callback():
    bot = FakeBot(error=RuntimeError("Bad Request: message to edit not found"))
    gone = []

    async def on_gone(chat_id, address):
        gone.append((chat_id, address))

    await LiveCardEditor(bot, on_message_gone=on_gone).edit(111, 500, NFT, "text")
    assert gone == [(111, NFT)]


async def test_blocked_chat_triggers_permanent_failure_callback():
    bot = FakeBot(error=RuntimeError("Forbidden: bot was blocked by the user"))
    deactivated = []

    async def on_fail(chat_id):
        deactivated.append(chat_id)

    await LiveCardEditor(bot, on_permanent_failure=on_fail).edit(111, 500, NFT, "text")
    assert deactivated == [111]


async def test_rate_limit_is_retried():
    class Limited(FakeBot):
        def __init__(self):
            super().__init__()
            self.first = True

        async def edit_message_text(self, **kwargs):
            self.calls.append(kwargs)
            if self.first:
                self.first = False
                exc = RuntimeError("Too Many Requests")
                exc.retry_after = 0
                raise exc

    bot = Limited()
    await LiveCardEditor(bot).edit(111, 500, NFT, "text")
    assert len(bot.calls) == 2


async def test_unknown_error_is_swallowed_without_crashing():
    bot = FakeBot(error=RuntimeError("something unexpected"))
    await LiveCardEditor(bot).edit(111, 500, NFT, "text")  # не должно бросить наружу


# --- перевод цены в доллары ---


def test_usd_price_is_shown_next_to_the_eth_price():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1, mint_price_wei=10**16), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
        usd_price="$30.00",
    )
    assert "$30.00" in text
    assert "0.01 ETH" in text


def test_usd_price_is_absent_by_default():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1, mint_price_wei=10**16), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "$" not in text


def test_usd_price_next_to_free_mint():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1, mint_price_wei=0), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
        usd_price="$2.15 с газом",
    )
    assert "бесплатно" in text
    assert "$2.15 с газом" in text


# --- статистика продаж ---


def test_volume_and_sales_are_shown_together():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
        total_volume=12.3, total_sales=40,
    )
    assert "12.3 ETH" in text
    assert "40 продаж" in text


def test_average_last_and_highest_sale_lines():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
        average_price=0.31, last_sale=0.25, highest_sale=0.5,
    )
    assert "0.31 ETH" in text
    assert "0.25 ETH" in text
    assert "0.5 ETH" in text
    assert "макс. продажа (из последних)" in text


def test_sale_stats_show_usd_estimates_when_given():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
        total_volume=12.3, volume_usd="$24600.00",
        average_price=0.31, average_usd="$620.00",
        last_sale=0.25, last_sale_usd="$500.00",
        highest_sale=0.5, highest_sale_usd="$1000.00",
    )
    assert "12.3 ETH (≈ $24600.00)" in text
    assert "0.31 ETH (≈ $620.00)" in text
    assert "0.25 ETH (≈ $500.00)" in text
    assert "0.5 ETH (≈ $1000.00)" in text


def test_sales_stats_are_absent_by_default():
    text = format_tracking_card(
        PROJECT, FakeInfo(total_supply=1), ProjectLinks(),
        floor=None, num_owners=None, now=NOW, sold_out=False,
    )
    assert "объём" not in text
    assert "продажа" not in text
