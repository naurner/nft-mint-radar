import pytest

from wlparser.sources.extract import (
    classify,
    extract_addresses,
    extract_marketplace_slugs,
    is_engagement_bait,
    is_memecoin_signal_spam,
)

ADDR = "0xb8a33b6e631773f962fe6ceea5db7746063670c9"


# --- адреса ---


def test_extracts_contract_address():
    assert extract_addresses(f"minting now at {ADDR} go") == [ADDR]


def test_address_is_lowercased_and_deduplicated():
    upper = "0xB8A33B6E631773F962FE6CEEA5DB7746063670C9"
    assert extract_addresses(f"{upper} and {ADDR}") == [ADDR]


def test_transaction_hash_is_not_mistaken_for_address():
    # Хеш транзакции — 64 символа; наивная регулярка вырезала бы из него адрес.
    tx = "0x" + "a" * 64
    assert extract_addresses(f"see tx {tx}") == []


def test_no_addresses_in_plain_text():
    assert extract_addresses("whitelist is open, join now") == []


def test_handles_empty_text():
    assert extract_addresses("") == []
    assert extract_addresses(None) == []


# --- маркетплейсы ---


def test_extracts_opensea_collection_slug():
    text = "check https://opensea.io/collection/clunker-hood now"
    assert extract_marketplace_slugs(text) == ["clunker-hood"]


def test_extracts_slug_from_assets_url():
    text = "https://opensea.io/assets/robinhood/onchain-harvest"
    assert "onchain-harvest" in extract_marketplace_slugs(text)


def test_ignores_unrelated_links():
    assert extract_marketplace_slugs("https://example.com/collection/x") == []


# --- накрутка активности ---


@pytest.mark.parametrize(
    "text",
    [
        "Follow me and RT to win WL",
        "RT + follow for a chance",
        "like + RT for whitelist",
        "tag 3 friends for WL",
        "drop your wallet below for the airdrop",
    ],
)
def test_engagement_bait_is_detected(text):
    assert is_engagement_bait(text) is True


def test_normal_announcement_is_not_bait():
    assert is_engagement_bait("Public mint is live, 0.01 ETH") is False


# --- классификация ---


@pytest.mark.parametrize(
    "text",
    [
        "Mint is live now!",
        "Public mint open for everyone",
        "WL raffle closes in 2 hours",
        "Free mint on Robinhood Chain",
        "Allowlist open for OG role holders",
    ],
)
def test_strong_phrases_mean_announcement(text):
    assert classify(text) == "wl_announced"


def test_weak_word_with_contract_address_is_announcement():
    assert classify(f"whitelist for this one {ADDR}") == "wl_announced"


def test_weak_word_with_opensea_link_is_announcement():
    text = "raffle for https://opensea.io/collection/clunker-hood"
    assert classify(text) == "wl_announced"


def test_bare_weak_word_is_only_chatter():
    # Слово «whitelist» само по себе не значит ничего.
    assert classify("i love whitelist season") == "chatter"


def test_engagement_bait_never_becomes_announcement():
    # Даже с сильной фразой внутри — это накрутка, а не анонс.
    assert classify("Mint is live! Follow and RT to enter") == "chatter"


def test_unrelated_text_is_chatter():
    assert classify("gm everyone, nice weather") == "chatter"


def test_wl_is_matched_as_a_word_not_a_fragment():
    assert classify(f"WL open {ADDR}") == "wl_announced"
    assert classify("owl sanctuary photos") == "chatter"


# --- боты-трейдсигналы по мемкоинам (не NFT), см. is_memecoin_signal_spam ---


def test_ai_signal_memecoin_bot_is_not_an_announcement():
    # Реальный пример: "mint authority" контракта токена — это право
    # эмиссии, не старт NFT-минта, но раньше classify() путал их из-за
    # слова "mint" рядом с адресом контракта.
    text = (
        f"AI Signal (RBH) $LONG\n\nCA: {ADDR}\n\n"
        "Eighteenth long-themed launch on Robinhood chain today.\n\n"
        "Mint and freeze authority is not renounced, so supply can still change.\n\n"
        "Smart money: five smart wallets bought at our signal.\n\n"
        "Not financial advice. Analysis only · DYOR"
    )
    assert classify(text) == "chatter"


def test_hour_update_memecoin_bot_is_not_an_announcement():
    text = (
        f"1-hour update $CHINAPAD\n\nCA: {ADDR}\n\n"
        "the mint is not renounced.\n\n"
        "Not financial advice. Analysis only · DYOR"
    )
    assert classify(text) == "chatter"


def test_single_memecoin_marker_alone_does_not_suppress_a_real_announcement():
    # Один случайный термин из словаря бота не должен гасить настоящий анонс.
    assert is_memecoin_signal_spam(f"DYOR but free mint is live {ADDR}") is False
    assert classify(f"DYOR but free mint is live {ADDR}") == "wl_announced"


def test_two_memecoin_markers_are_enough_to_flag_as_spam():
    assert is_memecoin_signal_spam("smart wallets say honeypot risk here") is True
