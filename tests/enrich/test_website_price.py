from wlparser.enrich.website_price import WebsitePriceLookup, extract_mint_price_eth


def test_finds_price_right_after_the_keyword():
    html = "<div>Mint Price: 0.05 ETH per NFT</div>"
    assert extract_mint_price_eth(html) == 0.05


def test_finds_price_with_the_currency_symbol_first():
    html = "<span>Price: ETH 0.08</span>"
    assert extract_mint_price_eth(html) == 0.08


def test_finds_price_with_the_eth_glyph():
    html = "<b>Cost: Ξ0.02</b>"
    assert extract_mint_price_eth(html) == 0.02


def test_recognizes_explicit_free_mint():
    html = "<h1>Mint Price: FREE</h1>"
    assert extract_mint_price_eth(html) == 0.0


def test_ignores_a_price_in_a_different_currency():
    # Только ETH имеет смысл на Robinhood Chain — шаблон для другой сети
    # (MATIC/SOL) не должен подставляться как будто это ETH.
    html = "<div>Mint Price: 5 MATIC</div>"
    assert extract_mint_price_eth(html) is None


def test_ignores_unrelated_numbers_on_the_page():
    html = "<p>Total supply 2222. Follow us on Twitter for updates.</p>"
    assert extract_mint_price_eth(html) is None


def test_ignores_numbers_inside_script_tags():
    # Иначе случайная цифра в JS-коде страницы выглядела бы как цена.
    html = "<script>var price = 999.0; var ETH = true;</script><p>Price: 0.03 ETH</p>"
    assert extract_mint_price_eth(html) == 0.03


def test_returns_none_for_empty_html():
    assert extract_mint_price_eth(None) is None
    assert extract_mint_price_eth("") is None


class FakeFetch:
    def __init__(self, status=200, html=""):
        self.status = status
        self.html = html
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        return self.status, self.html


async def test_lookup_returns_none_on_non_200():
    fetch = FakeFetch(status=404, html="<p>Price: 0.05 ETH</p>")
    lookup = WebsitePriceLookup(fetch)
    assert await lookup.mint_price_eth("https://example.com") is None


async def test_lookup_parses_the_fetched_page():
    fetch = FakeFetch(status=200, html="<p>Mint Price: 0.12 ETH</p>")
    lookup = WebsitePriceLookup(fetch)
    assert await lookup.mint_price_eth("https://example.com") == 0.12


async def test_lookup_returns_none_when_fetch_raises():
    # Раньше сетевая ошибка (DNS, таймаут) улетала наружу и валила весь
    # апдейт карточки проекта, а не только пропускала цену с сайта.
    async def broken_fetch(url):
        raise OSError("Name or service not known")

    lookup = WebsitePriceLookup(broken_fetch)
    assert await lookup.mint_price_eth("https://example.com") is None
