from wlparser.chain.contracts import ContractInspector
from wlparser.chain.selectors import (
    SEL_NAME,
    SEL_SUPPORTS_INTERFACE,
    SEL_SYMBOL,
    SEL_TOTAL_SUPPLY,
)

WORD_TRUE = "0x" + "0" * 63 + "1"


def string_word(text: str) -> str:
    word = lambda n: format(n, "064x")  # noqa: E731
    return "0x" + word(32) + word(len(text)) + text.encode().hex().ljust(64, "0")


class ScriptedClient:
    """Отвечает на eth_call по селектору, найденному в поле data."""

    def __init__(self, by_selector, *, erc721=False, erc1155=False):
        self.by_selector = by_selector
        self.erc721 = erc721
        self.erc1155 = erc1155

    async def batch(self, calls):
        results = []
        for _method, params in calls:
            data = params[0]["data"]
            if data.startswith(SEL_SUPPORTS_INTERFACE):
                if "80ac58cd" in data:
                    results.append(WORD_TRUE if self.erc721 else None)
                elif "d9b67a26" in data:
                    results.append(WORD_TRUE if self.erc1155 else None)
                else:
                    results.append(None)
            else:
                results.append(self.by_selector.get(data[:10]))
        return results


async def test_detects_erc721_and_reads_metadata():
    client = ScriptedClient(
        {
            SEL_NAME: string_word("Onchain Harvest"),
            SEL_SYMBOL: string_word("OCH"),
            SEL_TOTAL_SUPPLY: "0x" + format(1500, "064x"),
        },
        erc721=True,
    )
    info = await ContractInspector(client).inspect("0xABC")
    assert info.standard == "erc721"
    assert info.name == "Onchain Harvest"
    assert info.symbol == "OCH"
    assert info.total_supply == 1500
    assert info.address == "0xabc"


async def test_detects_erc1155():
    client = ScriptedClient({}, erc1155=True)
    info = await ContractInspector(client).inspect("0xabc")
    assert info.standard == "erc1155"


async def test_non_nft_contract_returns_none_standard():
    info = await ContractInspector(ScriptedClient({})).inspect("0xabc")
    assert info.standard is None
    assert info.is_nft is False


async def test_metadata_missing_does_not_break_detection():
    info = await ContractInspector(ScriptedClient({}, erc721=True)).inspect("0xabc")
    assert info.is_nft is True
    assert info.name is None
    assert info.label == "0xabc"


async def test_identification_does_not_fetch_optional_pricing():
    """Минтящихся контрактов сотни, почти все отсеиваются фильтром.

    Одиннадцать дополнительных вызовов на каждый — верный способ
    упереться в ограничение частоты запросов.
    """
    calls = []

    class Counting(ScriptedClient):
        async def batch(self, batch_calls):
            calls.append(len(batch_calls))
            return await super().batch(batch_calls)

    await ContractInspector(Counting({}, erc721=True)).inspect("0xabc")
    assert calls == [5]


async def test_with_pricing_reads_price_from_any_known_signature():
    from wlparser.chain.selectors import function_selector

    client = ScriptedClient(
        {function_selector("cost()"): "0x" + format(10**16, "064x")},
        erc721=True,
    )
    inspector = ContractInspector(client)
    info = await inspector.inspect("0xabc")
    assert info.mint_price_wei is None

    priced = await inspector.with_pricing(info)
    assert priced.mint_price_wei == 10**16
    assert priced.address == info.address
    assert priced.standard == "erc721"


async def test_with_pricing_skips_the_call_when_already_known():
    from wlparser.chain.contracts import ContractInfo

    calls = []

    class Counting(ScriptedClient):
        async def batch(self, batch_calls):
            calls.append(len(batch_calls))
            return await super().batch(batch_calls)

    known = ContractInfo(
        address="0xabc", standard="erc721", mint_price_wei=0, max_supply=100
    )
    same = await ContractInspector(Counting({})).with_pricing(known)
    assert same is known
    assert calls == []
