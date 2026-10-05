import pytest

from wlparser.enrich.models import (
    ProjectLinks,
    marketplace_url,
    safe_url,
    twitter_url,
)


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "data:text/html,x", "ftp://host/f", "", None, "   "],
)
def test_safe_url_rejects_non_http_schemes(value):
    # project_url заполняет посторонний человек — схему пускать нельзя.
    assert safe_url(value) is None


def test_safe_url_accepts_http_and_https():
    assert safe_url("https://a.example/") == "https://a.example/"
    assert safe_url(" http://a.example ") == "http://a.example"


@pytest.mark.parametrize("value", ["", None, "@", "https://x.com/foo", "a b", "x" * 25])
def test_twitter_url_rejects_garbage(value):
    assert twitter_url(value) is None


def test_twitter_url_builds_from_handle():
    assert twitter_url("@Clunkerhood") == "https://x.com/Clunkerhood"
    assert twitter_url("Clunkerhood") == "https://x.com/Clunkerhood"


def test_merged_with_fills_only_empty_fields():
    base = ProjectLinks(opensea="https://opensea.io/collection/x")
    other = ProjectLinks(opensea="https://other", explorer="https://scan/token/0x1")
    merged = base.merged_with(other)
    assert merged.opensea == "https://opensea.io/collection/x"
    assert merged.explorer == "https://scan/token/0x1"


def test_has_socials_is_false_for_explorer_only():
    assert ProjectLinks(explorer="https://scan/token/0x1").has_socials is False


def test_marketplace_url_is_built_from_the_contract():
    assert marketplace_url("robinhood", "0xAbC") == "https://opensea.io/assets/robinhood/0xabc"


def test_marketplace_url_needs_both_parts():
    assert marketplace_url("", "0xabc") is None
    assert marketplace_url("robinhood", "") is None
