from datetime import datetime, timedelta, timezone

from wlparser.pricing import (
    EthUsdRate,
    GasFeeEstimator,
    PriceConverter,
    format_usd_estimate,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- format_usd_estimate ---


def test_plain_conversion_without_gas():
    # 0.01 ETH при курсе $3000 -> $30.00
    assert format_usd_estimate(10**16, 3000.0, None) == "$30.00"


def test_conversion_with_gas_adds_the_fee():
    # 0.01 ETH минт + 0.005 ETH газа = 0.015 ETH -> $45.00 при курсе $3000
    assert format_usd_estimate(10**16, 3000.0, 5 * 10**15) == "$45.00 с газом"


def test_free_mint_with_gas_shows_only_the_gas_cost():
    # Реальная цена входа при "бесплатном" минте — это и есть газ.
    assert format_usd_estimate(0, 3000.0, 2 * 10**15) == "$6.00 с газом"


def test_free_mint_without_gas_is_zero():
    assert format_usd_estimate(0, 3000.0, None) == "$0.00"


def test_no_gas_line_omits_the_suffix():
    assert "с газом" not in format_usd_estimate(10**18, 2000.0, None)


# --- EthUsdRate ---


class FakeFetch:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, url, headers):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


async def test_rate_parses_coingecko_response():
    fetch = FakeFetch([(200, {"ethereum": {"usd": 3456.78}})])
    rate = EthUsdRate(fetch)
    assert await rate.get() == 3456.78


async def test_rate_is_cached_between_calls():
    fetch = FakeFetch([(200, {"ethereum": {"usd": 1000.0}})])
    rate = EthUsdRate(fetch, cache_seconds=60)
    await rate.get()
    await rate.get()
    assert fetch.calls == 1


async def test_rate_refreshes_after_cache_expires():
    clock = {"now": T0}
    fetch = FakeFetch(
        [(200, {"ethereum": {"usd": 1000.0}}), (200, {"ethereum": {"usd": 1100.0}})]
    )
    rate = EthUsdRate(fetch, cache_seconds=60, now=lambda: clock["now"])
    assert await rate.get() == 1000.0
    clock["now"] = T0 + timedelta(seconds=61)
    assert await rate.get() == 1100.0
    assert fetch.calls == 2


async def test_rate_falls_back_to_cached_value_on_network_error():
    class Failing:
        def __init__(self):
            self.calls = 0

        async def __call__(self, url, headers):
            self.calls += 1
            if self.calls == 1:
                return 200, {"ethereum": {"usd": 500.0}}
            raise RuntimeError("network down")

    fetch = Failing()
    clock = {"now": T0}
    rate = EthUsdRate(fetch, cache_seconds=1, now=lambda: clock["now"])
    assert await rate.get() == 500.0
    clock["now"] = T0 + timedelta(seconds=5)
    assert await rate.get() == 500.0  # сеть упала, но старое значение осталось


async def test_rate_returns_none_when_never_fetched_successfully():
    async def always_fails(url, headers):
        raise RuntimeError("down")

    rate = EthUsdRate(always_fails)
    assert await rate.get() is None


async def test_rate_ignores_malformed_response():
    fetch = FakeFetch([(200, {"unexpected": "shape"})])
    rate = EthUsdRate(fetch)
    assert await rate.get() is None


async def test_rate_ignores_non_200_status():
    fetch = FakeFetch([(429, None)])
    rate = EthUsdRate(fetch)
    assert await rate.get() is None


# --- GasFeeEstimator ---


class FakeRpc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def call(self, method, params=None):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


async def test_gas_estimate_multiplies_price_by_gas_units():
    client = FakeRpc([hex(20_000_000_000)])  # 20 gwei
    estimator = GasFeeEstimator(client, gas_per_mint=150_000)
    assert await estimator.estimate_fee_wei() == 20_000_000_000 * 150_000


async def test_gas_estimate_is_cached():
    client = FakeRpc([hex(10**10)])
    estimator = GasFeeEstimator(client, cache_seconds=60)
    await estimator.estimate_fee_wei()
    await estimator.estimate_fee_wei()
    assert client.calls == 1


async def test_gas_estimate_falls_back_on_rpc_failure():
    class Failing:
        def __init__(self):
            self.calls = 0

        async def call(self, method, params=None):
            self.calls += 1
            if self.calls == 1:
                return hex(10**10)
            raise RuntimeError("rpc down")

    client = Failing()
    clock = {"now": T0}
    estimator = GasFeeEstimator(client, cache_seconds=1, now=lambda: clock["now"])
    first = await estimator.estimate_fee_wei()
    clock["now"] = T0 + timedelta(seconds=5)
    assert await estimator.estimate_fee_wei() == first


async def test_gas_estimate_returns_none_when_never_succeeded():
    class AlwaysFails:
        async def call(self, method, params=None):
            raise RuntimeError("down")

    estimator = GasFeeEstimator(AlwaysFails())
    assert await estimator.estimate_fee_wei() is None


# --- PriceConverter ---


async def test_converter_combines_rate_and_gas():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))
    gas = GasFeeEstimator(FakeRpc([hex(10**9)]), gas_per_mint=100_000)
    converter = PriceConverter(rate, gas)
    line = await converter.usd_line(10**16)
    assert line is not None
    assert line.endswith("с газом")


async def test_converter_without_gas_estimator_still_converts():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))
    converter = PriceConverter(rate, None)
    assert await converter.usd_line(10**18) == "$2000.00"


async def test_converter_can_disable_gas_even_when_estimator_is_present():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))
    gas = GasFeeEstimator(FakeRpc([hex(10**9)]))
    converter = PriceConverter(rate, gas, include_gas=False)
    line = await converter.usd_line(10**18)
    assert "с газом" not in line


async def test_converter_returns_none_without_a_price():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))
    converter = PriceConverter(rate)
    assert await converter.usd_line(None) is None


async def test_converter_returns_none_when_rate_unavailable():
    async def always_fails(url, headers):
        raise RuntimeError("down")

    converter = PriceConverter(EthUsdRate(always_fails))
    assert await converter.usd_line(10**16) is None


async def test_converter_does_not_crash_when_gas_lookup_fails():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))

    class Failing:
        async def call(self, method, params=None):
            raise RuntimeError("down")

    converter = PriceConverter(rate, GasFeeEstimator(Failing()))
    # Газ недоступен -> просто конвертация без него, а не падение.
    assert await converter.usd_line(10**18) == "$2000.00"


# --- PriceConverter.usd_amount (без комиссии газа: флор, продажи) ---


async def test_usd_amount_converts_without_gas():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))
    converter = PriceConverter(rate, GasFeeEstimator(FakeRpc([hex(10**9)])))
    assert await converter.usd_amount(0.05) == "$100.00"


async def test_usd_amount_returns_none_without_a_value():
    rate = EthUsdRate(FakeFetch([(200, {"ethereum": {"usd": 2000.0}})]))
    converter = PriceConverter(rate)
    assert await converter.usd_amount(None) is None


async def test_usd_amount_returns_none_when_rate_unavailable():
    async def always_fails(url, headers):
        raise RuntimeError("down")

    converter = PriceConverter(EthUsdRate(always_fails))
    assert await converter.usd_amount(0.05) is None
