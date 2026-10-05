from wlparser.chain.probe import probe_endpoint


class ScriptedClient:
    def __init__(self, results):
        self.results = results

    async def call(self, method, params=None):
        value = self.results.get(method)
        if isinstance(value, Exception):
            raise value
        return value


async def test_healthy_endpoint_is_usable():
    client = ScriptedClient(
        {"eth_chainId": "0x1b3f", "eth_blockNumber": "0x2710", "eth_getLogs": []}
    )
    result = await probe_endpoint(client)
    assert result.usable is True
    assert result.chain_id == 6975
    assert result.block_number == 10000


async def test_endpoint_without_getlogs_is_not_usable():
    # Без eth_getLogs минты не поймать — такой эндпоинт бесполезен.
    client = ScriptedClient(
        {
            "eth_chainId": "0x1b3f",
            "eth_blockNumber": "0x2710",
            "eth_getLogs": RuntimeError("method not supported"),
        }
    )
    result = await probe_endpoint(client)
    assert result.usable is False
    assert "eth_getLogs" in result.problem


async def test_unreachable_endpoint_reports_problem():
    client = ScriptedClient({"eth_chainId": RuntimeError("connection refused")})
    result = await probe_endpoint(client)
    assert result.usable is False
    assert "connection refused" in result.problem
