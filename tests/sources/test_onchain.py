import asyncio

from wlparser.chain.selectors import (
    SEL_NAME,
    SEL_SUPPORTS_INTERFACE,
    SEL_SYMBOL,
    TRANSFER_SINGLE_TOPIC,
    TRANSFER_TOPIC,
    ZERO_TOPIC,
)
from wlparser.sources.onchain import OnchainSource

NFT = "0x" + "a" * 40
OTHER = "0x" + "c" * 40
WORD_TRUE = "0x" + "0" * 63 + "1"


def string_word(text: str) -> str:
    word = lambda n: format(n, "064x")  # noqa: E731
    return "0x" + word(32) + word(len(text)) + text.encode().hex().ljust(64, "0")


def erc721_mint(address: str, to: int = 1):
    return {
        "address": address,
        "topics": [
            TRANSFER_TOPIC,
            ZERO_TOPIC,
            "0x" + "0" * 24 + format(to, "040x"),
            "0x" + format(to, "064x"),
        ],
    }


def erc1155_mint(address: str, to: int = 1):
    return {
        "address": address,
        "topics": [
            TRANSFER_SINGLE_TOPIC,
            "0x" + "0" * 24 + "9" * 40,  # operator
            ZERO_TOPIC,  # from
            "0x" + "0" * 24 + format(to, "040x"),  # to
        ],
    }


class FakeChain:
    """Отдаёт заранее заданные логи и метаданные контрактов."""

    def __init__(self, logs_721=(), logs_1155=(), nfts=None, head=10_000):
        self.logs_721 = list(logs_721)
        self.logs_1155 = list(logs_1155)
        self.nfts = {k.lower(): v for k, v in (nfts or {}).items()}
        self.head = head
        self.log_requests = []
        self.batch_calls = 0

    async def call(self, method, params=None):
        if method == "eth_blockNumber":
            return hex(self.head)
        raise AssertionError(f"неожиданный метод {method}")

    async def batch(self, calls):
        # Запрос логов диапазона — это батч из двух eth_getLogs.
        if calls and calls[0][0] == "eth_getLogs":
            self.log_requests.append(
                (int(calls[0][1][0]["fromBlock"], 16), int(calls[0][1][0]["toBlock"], 16))
            )
            return [self.logs_721, self.logs_1155]

        self.batch_calls += 1
        out = []
        for _method, params in calls:
            target = params[0]["to"].lower()
            data = params[0]["data"]
            name = self.nfts.get(target)
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


async def drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def make_source(chain, **kwargs):
    kwargs.setdefault("min_unique_minters", 3)
    kwargs.setdefault("start_block", 100)
    return OnchainSource(chain, chain_name="robinhood", **kwargs)


# --- фильтр запроса логов ---


async def test_log_query_filters_zero_sender_server_side():
    """Из десятков тысяч переводов сеть должна вернуть только минты."""
    captured = {}

    class Capturing(FakeChain):
        async def batch(self, calls):
            if calls and calls[0][0] == "eth_getLogs":
                captured["erc721"] = calls[0][1][0]["topics"]
                captured["erc1155"] = calls[1][1][0]["topics"]
            return await super().batch(calls)

    await make_source(Capturing()).process_range(100, 200, asyncio.Queue())
    assert captured["erc721"] == [TRANSFER_TOPIC, ZERO_TOPIC]
    # У ERC-1155 отправитель третий: первый — operator.
    assert captured["erc1155"] == [TRANSFER_SINGLE_TOPIC, None, ZERO_TOPIC]


async def test_range_is_requested_as_a_whole_not_block_by_block():
    chain = FakeChain()
    await make_source(chain).process_range(100, 599, asyncio.Queue())
    assert chain.log_requests == [(100, 599)]


# --- минты ---


async def test_single_mint_produces_no_alert():
    chain = FakeChain([erc721_mint(NFT)], nfts={NFT: "Pilao gua"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    assert queue.empty()


async def test_alert_once_enough_distinct_wallets_minted():
    logs = [erc721_mint(NFT, to=i) for i in range(1, 4)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    [sig] = await drain(queue)
    assert sig.kind == "mint_started"
    assert "Cash Bears" in sig.text
    assert "3 wallets" in sig.text


async def test_erc1155_mints_are_counted_too():
    logs = [erc1155_mint(NFT, to=i) for i in range(1, 4)]
    chain = FakeChain(logs_1155=logs, nfts={NFT: "Cash Bears"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    [sig] = await drain(queue)
    assert sig.raw["unique_minters"] == 3


async def test_same_wallet_repeating_never_triggers_alert():
    logs = [erc721_mint(NFT, to=7) for _ in range(40)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    assert queue.empty()


async def test_infrastructure_nft_is_ignored_entirely():
    logs = [erc721_mint(NFT, to=i) for i in range(1, 20)]
    chain = FakeChain(logs, nfts={NFT: "Uniswap v4 Positions NFT"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    assert queue.empty()


async def test_non_nft_contract_is_ignored():
    chain = FakeChain([erc721_mint(NFT, to=i) for i in range(1, 20)], nfts={})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    assert queue.empty()


async def test_alert_is_not_repeated_on_later_ranges():
    logs = [erc721_mint(NFT, to=i) for i in range(1, 10)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    source = make_source(chain)
    queue = asyncio.Queue()
    await source.process_range(100, 200, queue)
    await source.process_range(201, 300, queue)
    assert queue.qsize() == 1


async def test_contract_is_identified_once_then_taken_from_cache():
    """Опознание — один запрос, дальше кеш; второй запрос это добор цены."""
    logs = [erc721_mint(NFT, to=i) for i in range(1, 5)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    source = make_source(chain)
    queue = asyncio.Queue()
    await source.process_range(100, 200, queue)
    after_first = chain.batch_calls
    await source.process_range(201, 300, queue)
    # Опознание + добор цены при алерте, и ничего нового на втором диапазоне.
    assert after_first == 2
    assert chain.batch_calls == 2


async def test_pricing_is_not_fetched_for_contracts_below_threshold():
    # Один минт порога не достигает — платить за цену и лимит не за что.
    chain = FakeChain([erc721_mint(NFT)], nfts={NFT: "Cash Bears"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    assert chain.batch_calls == 1


async def test_rejected_contract_is_not_re_inspected():
    logs = [erc721_mint(NFT, to=i) for i in range(1, 5)]
    chain = FakeChain(logs, nfts={NFT: "Uniswap Positions"})
    source = make_source(chain)
    queue = asyncio.Queue()
    await source.process_range(100, 200, queue)
    await source.process_range(201, 300, queue)
    assert chain.batch_calls == 1


async def test_several_collections_in_one_range_are_separated():
    logs = [erc721_mint(NFT, to=i) for i in range(1, 4)]
    logs += [erc721_mint(OTHER, to=i) for i in range(10, 13)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears", OTHER: "Onchain Harvest"})
    queue = asyncio.Queue()
    await make_source(chain).process_range(100, 200, queue)
    assert {s.contract_address for s in await drain(queue)} == {NFT, OTHER}


# --- всплеск ---


async def test_surge_does_not_duplicate_the_mint_alert_in_the_same_range():
    """«Начался минт» и «всплеск» об одном событии — это одно сообщение."""
    logs = [erc721_mint(NFT, to=i) for i in range(1, 31)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    queue = asyncio.Queue()
    await make_source(chain, surge_threshold=25).process_range(100, 200, queue)
    assert [s.kind for s in await drain(queue)] == ["mint_started"]


async def test_later_surge_is_still_reported():
    """Проект, о котором уже сообщали, может разогнаться позже.

    Это другая новость, и придерживать её нельзя.
    """
    logs = [erc721_mint(NFT, to=i) for i in range(1, 31)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    source = make_source(chain, surge_threshold=25)
    queue = asyncio.Queue()
    await source.process_range(100, 200, queue)   # порог взят -> mint_started
    await source.process_range(201, 300, queue)   # разгон -> mint_surge
    assert [s.kind for s in await drain(queue)] == ["mint_started", "mint_surge"]


async def test_surge_is_reported_only_once_afterwards():
    logs = [erc721_mint(NFT, to=i) for i in range(1, 31)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    source = make_source(chain, surge_threshold=25)
    queue = asyncio.Queue()
    for start in (100, 201, 301, 401):
        await source.process_range(start, start + 100, queue)
    assert [s.kind for s in await drain(queue)] == ["mint_started", "mint_surge"]


async def test_one_wallet_buying_everything_is_not_a_surge():
    logs = [erc721_mint(NFT, to=7) for _ in range(40)]
    chain = FakeChain(logs, nfts={NFT: "Cash Bears"})
    queue = asyncio.Queue()
    await make_source(chain, surge_threshold=25).process_range(100, 200, queue)
    assert queue.empty()


# --- продвижение и отставание ---


async def test_advances_in_ranges_behind_confirmations():
    chain = FakeChain(head=1000)
    source = make_source(chain, log_range_blocks=200, confirmations=2, poll_interval=0.01)
    queue = asyncio.Queue()
    task = asyncio.create_task(source.run(queue))
    await asyncio.sleep(0.1)
    task.cancel()
    assert source.next_block == 999  # голова 1000 - 2 подтверждения + 1


async def test_hopeless_lag_skips_to_the_present():
    """Сутки простоя дают почти миллион блоков — разбирать их бессмысленно."""
    chain = FakeChain(head=1_000_000)
    source = make_source(
        chain, start_block=100, max_lag_blocks=5000, log_range_blocks=500
    )
    source._skip_ahead_if_hopeless(999_998)
    assert source.next_block == 999_498


async def test_small_lag_is_processed_normally():
    chain = FakeChain(head=1000)
    source = make_source(chain, start_block=900, max_lag_blocks=5000)
    source._skip_ahead_if_hopeless(998)
    assert source.next_block == 900


async def test_range_size_is_capped_at_endpoint_limit():
    # Эндпоинт отклоняет запросы шире 1000 блоков.
    source = make_source(FakeChain(), log_range_blocks=99_999)
    assert source._range == 1000
