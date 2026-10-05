import pytest

from wlparser.chain.rpc import JsonRpcClient, RpcError


class FakeTransport:
    """Отдаёт заранее заданные ответы и запоминает запросы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __call__(self, payload):
        self.requests.append(payload)
        return self.responses.pop(0)


async def test_call_returns_result():
    transport = FakeTransport([{"jsonrpc": "2.0", "id": 1, "result": "0x10"}])
    client = JsonRpcClient(transport)
    assert await client.call("eth_blockNumber") == "0x10"


async def test_call_sends_method_and_params():
    transport = FakeTransport([{"jsonrpc": "2.0", "id": 1, "result": "0x1"}])
    client = JsonRpcClient(transport)
    await client.call("eth_getBlockByNumber", ["0x1", True])
    assert transport.requests[0]["method"] == "eth_getBlockByNumber"
    assert transport.requests[0]["params"] == ["0x1", True]


async def test_call_raises_on_rpc_error():
    transport = FakeTransport(
        [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}]
    )
    client = JsonRpcClient(transport)
    with pytest.raises(RpcError, match="boom"):
        await client.call("eth_blockNumber")


async def test_batch_preserves_request_order_despite_shuffled_ids():
    # Спека JSON-RPC не гарантирует порядок ответов в батче.
    transport = FakeTransport(
        [
            [
                {"jsonrpc": "2.0", "id": 2, "result": "second"},
                {"jsonrpc": "2.0", "id": 1, "result": "first"},
            ]
        ]
    )
    client = JsonRpcClient(transport)
    assert await client.batch([("eth_a", []), ("eth_b", [])]) == ["first", "second"]


async def test_batch_returns_none_for_failed_entries():
    # Ревертнувшийся вызов — норма при опросе контракта, а не сбой батча.
    transport = FakeTransport(
        [
            [
                {"jsonrpc": "2.0", "id": 1, "result": "ok"},
                {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "rv"}},
            ]
        ]
    )
    client = JsonRpcClient(transport)
    assert await client.batch([("eth_a", []), ("eth_b", [])]) == ["ok", None]


async def test_empty_batch_makes_no_request():
    transport = FakeTransport([])
    client = JsonRpcClient(transport)
    assert await client.batch([]) == []
    assert transport.requests == []
