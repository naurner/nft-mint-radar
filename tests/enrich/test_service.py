import asyncio

from wlparser.enrich.models import ProjectLinks
from wlparser.enrich.service import Enricher, build_explorer_url

ADDRESS = "0xB8A33b6e631773F962fE6cEEa5Db7746063670c9"


class FakeOpenSea:
    def __init__(self, links=None, *, delay=0.0, error=None):
        self.links = links or ProjectLinks()
        self.delay = delay
        self.error = error
        self.calls = 0

    async def lookup(self, address):
        self.calls += 1
        if self.error:
            raise self.error
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.links


def make(opensea=None, **kwargs):
    kwargs.setdefault("explorer_url", "https://robinscan.io")
    return Enricher(opensea, **kwargs)


def test_build_explorer_url_uses_token_path():
    url = build_explorer_url("https://robinscan.io/", "/token/{address}", "0xabc")
    assert url == "https://robinscan.io/token/0xabc"


def test_build_explorer_url_empty_base_gives_none():
    assert build_explorer_url("", "/token/{address}", "0xabc") is None


async def test_explorer_link_is_always_present():
    links = await make().enrich(ADDRESS)
    assert links.explorer == f"https://robinscan.io/token/{ADDRESS.lower()}"


async def test_no_address_gives_empty_links():
    assert (await make().enrich(None)).explorer is None


async def test_opensea_links_are_merged_in():
    opensea = FakeOpenSea(
        ProjectLinks(opensea="https://opensea.io/collection/x", twitter="https://x.com/a")
    )
    links = await make(opensea).enrich(ADDRESS)
    assert links.opensea == "https://opensea.io/collection/x"
    assert links.twitter == "https://x.com/a"
    assert links.explorer is not None


async def test_result_is_cached_per_contract():
    opensea = FakeOpenSea(ProjectLinks(opensea="https://opensea.io/collection/x"))
    enricher = make(opensea)
    await enricher.enrich(ADDRESS)
    await enricher.enrich(ADDRESS.lower())
    assert opensea.calls == 1


async def test_slow_opensea_does_not_block_the_alert():
    # Лучше отправить без соцсетей, чем задержать уведомление о минте.
    opensea = FakeOpenSea(ProjectLinks(twitter="https://x.com/slow"), delay=0.5)
    links = await make(opensea, timeout_s=0.05).enrich(ADDRESS)
    assert links.twitter is None
    assert links.explorer is not None


async def test_opensea_failure_does_not_break_enrichment():
    opensea = FakeOpenSea(error=RuntimeError("boom"))
    links = await make(opensea).enrich(ADDRESS)
    assert links.explorer is not None
    assert links.twitter is None


async def test_marketplace_link_survives_a_dead_api():
    """OpenSea закрыл API за ключ — ссылка на коллекцию нужна всё равно.

    Она строится из адреса контракта локально, без единого запроса.
    """
    links = await make(FakeOpenSea(error=RuntimeError("401"))).enrich(ADDRESS)
    assert links.opensea == f"https://opensea.io/assets/robinhood/{ADDRESS.lower()}"


async def test_api_answer_wins_over_the_built_link():
    # Настоящий адрес коллекции точнее, чем переадресация по контракту.
    opensea = FakeOpenSea(ProjectLinks(opensea="https://opensea.io/collection/real"))
    links = await make(opensea).enrich(ADDRESS)
    assert links.opensea == "https://opensea.io/collection/real"


async def test_links_are_persisted_when_socials_found():
    saved = {}

    async def store(address, links):
        saved[address] = links

    opensea = FakeOpenSea(ProjectLinks(twitter="https://x.com/a"))
    await make(opensea, store=store).enrich(ADDRESS)
    assert saved[ADDRESS.lower()].twitter == "https://x.com/a"


async def test_nothing_persisted_when_opensea_knows_nothing():
    saved = {}

    async def store(address, links):
        saved[address] = links

    await make(FakeOpenSea(), store=store).enrich(ADDRESS)
    assert saved == {}


async def test_store_failure_does_not_break_enrichment():
    async def store(address, links):
        raise RuntimeError("disk full")

    opensea = FakeOpenSea(ProjectLinks(twitter="https://x.com/a"))
    links = await make(opensea, store=store).enrich(ADDRESS)
    assert links.twitter == "https://x.com/a"
