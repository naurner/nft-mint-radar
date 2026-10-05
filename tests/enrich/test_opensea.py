"""Ответы в фикстурах повторяют реальную форму OpenSea API v2."""

from wlparser.enrich.opensea import OpenSeaClient

ADDRESS = "0xb8a33b6e631773f962fe6ceea5db7746063670c9"

CONTRACT_RESPONSE = {
    "address": ADDRESS,
    "chain": "robinhood",
    "collection": "clunker-hood",
    "contract_standard": "erc721",
    "name": "Clunkerhood",
}

COLLECTION_RESPONSE = {
    "collection": "clunker-hood",
    "name": "Clunkerhood",
    "description": "6,399 beaters — the cars nobody would drive.",
    "project_url": "https://www.clunkerhood.xyz/",
    "discord_url": "",
    "telegram_url": "",
    "twitter_username": "Clunkerhood",
    "opensea_url": "https://opensea.io/collection/clunker-hood",
    "total_supply": 6371,
}


class FakeFetch:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = []

    async def __call__(self, url, headers):
        self.calls.append(url)
        self.headers.append(headers)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return 404, {"error": "not found"}


def client(routes, **kwargs):
    fetch = FakeFetch(routes)
    return OpenSeaClient(fetch, **kwargs), fetch


async def test_lookup_returns_all_links():
    api, _ = client(
        {
            f"/contract/{ADDRESS}": (200, CONTRACT_RESPONSE),
            "/collections/clunker-hood": (200, COLLECTION_RESPONSE),
        }
    )
    links = await api.lookup(ADDRESS)
    assert links.opensea == "https://opensea.io/collection/clunker-hood"
    assert links.twitter == "https://x.com/Clunkerhood"
    assert links.website == "https://www.clunkerhood.xyz/"
    assert links.collection_name == "Clunkerhood"
    assert links.total_supply == 6371
    assert links.has_socials is True


async def test_empty_social_fields_become_none():
    api, _ = client(
        {
            f"/contract/{ADDRESS}": (200, CONTRACT_RESPONSE),
            "/collections/clunker-hood": (200, COLLECTION_RESPONSE),
        }
    )
    links = await api.lookup(ADDRESS)
    assert links.discord is None
    assert links.telegram is None


async def test_unknown_contract_returns_empty_links():
    # Свежий контракт OpenSea ещё не проиндексировал — это норма.
    api, fetch = client({})
    links = await api.lookup(ADDRESS)
    assert links.opensea is None
    assert links.has_socials is False
    assert len(fetch.calls) == 1  # за коллекцией не ходили


async def test_api_key_is_sent_when_configured():
    api, fetch = client({f"/contract/{ADDRESS}": (200, CONTRACT_RESPONSE)}, api_key="k")
    await api.collection_slug(ADDRESS)
    assert fetch.headers[0]["x-api-key"] == "k"


async def test_no_api_key_header_by_default():
    api, fetch = client({f"/contract/{ADDRESS}": (200, CONTRACT_RESPONSE)})
    await api.collection_slug(ADDRESS)
    assert "x-api-key" not in fetch.headers[0]


async def test_chain_is_used_in_url():
    api, fetch = client({}, chain="robinhood")
    await api.collection_slug(ADDRESS)
    assert "/chain/robinhood/contract/" in fetch.calls[0]


async def test_rate_limited_response_yields_empty_links():
    api, _ = client({f"/contract/{ADDRESS}": (429, None)})
    assert (await api.lookup(ADDRESS)).opensea is None


async def test_opensea_url_falls_back_to_slug():
    response = dict(COLLECTION_RESPONSE, opensea_url="")
    api, _ = client(
        {
            f"/contract/{ADDRESS}": (200, CONTRACT_RESPONSE),
            "/collections/clunker-hood": (200, response),
        }
    )
    links = await api.lookup(ADDRESS)
    assert links.opensea == "https://opensea.io/collection/clunker-hood"


async def test_missing_api_key_is_reported_once(caplog):
    """401 не должен повторяться в логе на каждой находке."""
    api, _ = client({f"/contract/{ADDRESS}": (401, {"errors": ["Missing an API Key"]})})
    with caplog.at_level("WARNING"):
        await api.lookup(ADDRESS)
        await api.lookup(ADDRESS)
    warnings = [r for r in caplog.records if "API-ключ" in r.getMessage()]
    assert len(warnings) == 1
    assert "opensea.io/account/developer" in warnings[0].getMessage()


async def test_locked_api_yields_empty_links_without_raising():
    api, _ = client({f"/contract/{ADDRESS}": (401, None)})
    links = await api.lookup(ADDRESS)
    assert links.has_socials is False


# --- collection_slug сохраняется в результате lookup ---


async def test_lookup_includes_collection_slug():
    api, _ = client(
        {
            f"/contract/{ADDRESS}": (200, CONTRACT_RESPONSE),
            "/collections/clunker-hood": (200, COLLECTION_RESPONSE),
        }
    )
    links = await api.lookup(ADDRESS)
    assert links.collection_slug == "clunker-hood"


# --- collection_stats: флор и держатели для живого слежения ---

# Форма ответа проверена вживую 2026-09-13: average_price/market_cap в
# реальном API отсутствуют, зато floor_price_symbol и volume_symbol не
# всегда "ETH" — у части коллекций флор торгуется в стейблкоине.
STATS_RESPONSE = {
    "total": {
        "floor_price": 0.05,
        "floor_price_symbol": "ETH",
        "num_owners": 87,
        "volume": 12.3,
        "volume_symbol": "ETH",
        "sales": 40,
    },
}


async def test_collection_stats_returns_floor_and_owners():
    api, _ = client({"/collections/clunker-hood/stats": (200, STATS_RESPONSE)})
    stats = await api.collection_stats("clunker-hood")
    assert stats == {
        "floor_price": 0.05,
        "floor_symbol": "ETH",
        "num_owners": 87,
        "total_volume": 12.3,
        "volume_symbol": "ETH",
        "total_sales": 40,
        "average_price": 12.3 / 40,
    }


async def test_collection_stats_floor_kept_as_eth_not_converted_to_wei():
    # OpenSea отдаёт число в своей валюте, а не в wei — пересчёт float
    # внёс бы ошибку округления, которой не было бы без пересчёта.
    api, _ = client({"/collections/x/stats": (200, {"total": {"floor_price": 1.5}})})
    stats = await api.collection_stats("x")
    assert stats["floor_price"] == 1.5


async def test_collection_stats_defaults_symbol_to_eth_when_missing():
    api, _ = client({"/collections/x/stats": (200, {"total": {"floor_price": 1.5}})})
    stats = await api.collection_stats("x")
    assert stats["floor_symbol"] == "ETH"


async def test_collection_stats_reports_the_real_currency():
    # Живой пример: у Clunkerhood флор оказался в USDG, не в ETH.
    api, _ = client(
        {"/collections/x/stats": (200, {"total": {"floor_price": 0.1, "floor_price_symbol": "USDG"}})}
    )
    stats = await api.collection_stats("x")
    assert stats["floor_symbol"] == "USDG"


async def test_collection_stats_average_price_is_derived_not_from_the_api():
    # OpenSea это поле не отдаёт вовсе — считаем сами как volume / sales.
    api, _ = client({"/collections/x/stats": (200, {"total": {"volume": 10.0, "sales": 4}})})
    stats = await api.collection_stats("x")
    assert stats["average_price"] == 2.5


async def test_collection_stats_average_price_is_none_without_sales():
    api, _ = client({"/collections/x/stats": (200, {"total": {"volume": 10.0, "sales": 0}})})
    stats = await api.collection_stats("x")
    assert stats["average_price"] is None


async def test_collection_stats_missing_fields_are_none():
    api, _ = client({"/collections/x/stats": (200, {"total": {}})})
    stats = await api.collection_stats("x")
    assert stats == {
        "floor_price": None,
        "floor_symbol": "ETH",
        "num_owners": None,
        "total_volume": None,
        "volume_symbol": "ETH",
        "total_sales": None,
        "average_price": None,
    }


async def test_collection_stats_unknown_slug_returns_none():
    api, _ = client({})
    assert await api.collection_stats("ghost") is None


async def test_collection_stats_locked_api_returns_none_without_raising():
    api, _ = client({"/collections/x/stats": (401, None)})
    assert await api.collection_stats("x") is None


async def test_collection_stats_missing_key_warning_shares_the_flag_with_contract_lookup(caplog):
    # Один и тот же клиент — предупреждение о ключе всё равно одно на весь клиент.
    api, _ = client({f"/contract/{ADDRESS}": (401, None), "/collections/x/stats": (401, None)})
    with caplog.at_level("WARNING"):
        await api.collection_slug(ADDRESS)
        await api.collection_stats("x")
    warnings = [r for r in caplog.records if "API-ключ" in r.getMessage()]
    assert len(warnings) == 1


# --- collection_recent_sales: последняя и максимальная цена ---


def sale_event(price: float, *, timestamp=None, symbol="ETH", decimals=18):
    return {
        "event_type": "sale",
        "event_timestamp": timestamp,
        "payment": {
            "quantity": str(int(price * 10**decimals)),
            "decimals": decimals,
            "symbol": symbol,
        },
    }


async def test_recent_sales_reports_last_and_highest():
    api, _ = client(
        {
            "/events/collection/clunker-hood": (
                200,
                {
                    "asset_events": [
                        sale_event(0.10, timestamp=100),
                        sale_event(0.25, timestamp=200),  # позже по времени
                        sale_event(0.05, timestamp=50),
                    ]
                },
            )
        }
    )
    result = await api.collection_recent_sales("clunker-hood")
    assert result == {
        "last_sale": 0.25,
        "last_sale_symbol": "ETH",
        "highest_sale": 0.25,
        "highest_sale_symbol": "ETH",
        "sample_size": 3,
    }


async def test_recent_sales_highest_can_differ_from_last():
    api, _ = client(
        {
            "/events/collection/x": (
                200,
                {
                    "asset_events": [
                        sale_event(0.50, timestamp=100),  # самая дорогая, но не последняя
                        sale_event(0.10, timestamp=200),  # последняя по времени
                    ]
                },
            )
        }
    )
    result = await api.collection_recent_sales("x")
    assert result["last_sale"] == 0.10
    assert result["highest_sale"] == 0.50


async def test_recent_sales_without_timestamps_falls_back_to_response_order():
    api, _ = client(
        {
            "/events/collection/x": (
                200,
                {"asset_events": [sale_event(0.20), sale_event(0.40)]},
            )
        }
    )
    result = await api.collection_recent_sales("x")
    assert result["last_sale"] == 0.20  # первое в ответе, раз меток времени нет
    assert result["highest_sale"] == 0.40


async def test_recent_sales_each_carries_its_own_currency():
    # Живой пример: продажи одной и той же коллекции идут в USDG,
    # не в ETH — цена и подпись должны соответствовать друг другу.
    api, _ = client(
        {
            "/events/collection/x": (
                200,
                {
                    "asset_events": [
                        # Живая продажа Clunkerhood: quantity="60000", decimals=6 -> 0.06.
                        sale_event(0.06, timestamp=1, symbol="USDG", decimals=6),
                    ]
                },
            )
        }
    )
    result = await api.collection_recent_sales("x")
    assert result["last_sale"] == 0.06
    assert result["last_sale_symbol"] == "USDG"


async def test_recent_sales_ignores_events_without_a_price():
    api, _ = client(
        {
            "/events/collection/x": (
                200,
                {"asset_events": [{"event_type": "sale"}, sale_event(0.15)]},
            )
        }
    )
    result = await api.collection_recent_sales("x")
    assert result["sample_size"] == 1


async def test_recent_sales_no_events_returns_none():
    api, _ = client({"/events/collection/x": (200, {"asset_events": []})})
    assert await api.collection_recent_sales("x") is None


async def test_recent_sales_unknown_slug_returns_none():
    api, _ = client({})
    assert await api.collection_recent_sales("ghost") is None


async def test_recent_sales_locked_api_returns_none_without_raising():
    api, _ = client({"/events/collection/x": (401, None)})
    assert await api.collection_recent_sales("x") is None


async def test_recent_sales_respects_limit_in_the_request():
    api, fetch = client({"/events/collection/x": (200, {"asset_events": []})})
    await api.collection_recent_sales("x", limit=25)
    assert "limit=25" in fetch.calls[0]
