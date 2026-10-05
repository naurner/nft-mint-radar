from wlparser.poster.caption import (
    HASHTAGS,
    MAX_LEN,
    SKIP_TOKEN,
    body_budget,
    build_caption,
    caption_prompt,
    clean_generated_body,
    compose,
    is_skip_reply,
    quote_budget,
    quote_prompt,
)


def test_builds_a_caption_for_an_onchain_mint_surge():
    caption = build_caption(
        source="onchain",
        kind="mint_surge",
        text="Mint surge: Cash Bears, 94 wallets, 94 minted in the last blocks",
        opensea_url="https://opensea.io/assets/robinhood/0xabc",
    )
    assert caption is not None
    assert "Cash Bears" in caption
    assert "https://opensea.io/assets/robinhood/0xabc" in caption
    assert HASHTAGS in caption


def test_twitter_sourced_signals_are_never_posted():
    # Основная гарантия безопасности: никогда не пересказывать чужой твит.
    caption = build_caption(
        source="twitter", kind="wl_announced", text="great WL coming soon!"
    )
    assert caption is None


def test_chatter_and_wl_announced_are_not_postable_even_from_onchain():
    assert build_caption(source="onchain", kind="chatter", text="x") is None
    assert build_caption(source="onchain", kind="contract_deployed", text="x") is None


def test_mint_started_alone_is_no_longer_postable():
    # Слишком частый случай (десятки в час) — постим только более
    # доказанные сигналы: всплеск и почти распродано.
    assert build_caption(source="onchain", kind="mint_started", text="Mint confirmed: X") is None


def test_mint_surge_and_near_sellout_are_postable():
    assert build_caption(source="onchain", kind="mint_surge", text="Surge!") is not None
    assert (
        build_caption(source="onchain", kind="mint_near_sellout", text="90%!") is not None
    )


def test_caption_without_opensea_link_still_has_hashtags():
    caption = build_caption(source="onchain", kind="mint_surge", text="Mint surge: X")
    assert caption is not None
    assert HASHTAGS in caption


def test_caption_never_exceeds_the_tweet_length_limit():
    long_text = "Mint surge: " + "A" * 400
    caption = build_caption(
        source="onchain",
        kind="mint_surge",
        text=long_text,
        opensea_url="https://opensea.io/assets/robinhood/0xabc",
    )
    assert caption is not None
    assert len(caption) <= MAX_LEN
    assert "https://opensea.io/assets/robinhood/0xabc" in caption
    assert HASHTAGS in caption
    assert caption.startswith("Mint surge:")


def test_caption_trims_with_an_ellipsis_when_too_long():
    long_text = "X" * 400
    caption = build_caption(source="onchain", kind="mint_surge", text=long_text)
    assert caption is not None
    assert "…" in caption


# --- текст от локальной модели ---

FACT = "Mint confirmed: Cash Bears, 25 wallets, 30 minted in ~3 min, max supply 2222"
URL = "https://opensea.io/assets/robinhood/0xabc"


def test_compose_appends_link_and_hashtags():
    text = compose("Cash Bears is minting", URL)
    assert text.startswith("Cash Bears is minting\n\n")
    assert URL in text
    assert text.endswith(HASHTAGS)


def test_composed_caption_fits_when_body_uses_the_whole_budget():
    body = "x" * body_budget(URL)
    assert len(compose(body, URL)) == MAX_LEN


def test_prompt_carries_the_fact_and_the_budget():
    system, user = caption_prompt(FACT, 200)
    assert "200" in system
    assert FACT in user


def test_prompt_rules_hold_for_every_voice_variant():
    # Голос рандомный, но ограничения (бюджет, запрет хэштегов/чисел от
    # себя) должны быть в каждом варианте — проверяем много попыток, чтобы
    # с высокой вероятностью задеть все варианты хотя бы раз.
    for _ in range(50):
        system, _ = caption_prompt(FACT, 200)
        assert "200" in system
        assert "no hashtags" in system
        assert "no links" in system
        assert "@mentions" in system


def test_prompt_uses_more_than_one_voice():
    systems = {caption_prompt(FACT, 200)[0] for _ in range(50)}
    assert len(systems) > 1


def test_clean_accepts_a_faithful_body():
    body = "Cash Bears: 25 wallets minted 30 in ~3 min, max supply 2222."
    assert clean_generated_body(body, fact=FACT, budget=200) == body


def test_clean_strips_quotes_and_collapses_whitespace():
    raw = '"Cash Bears is live —\n25 wallets already."'
    assert (
        clean_generated_body(raw, fact=FACT, budget=200)
        == "Cash Bears is live — 25 wallets already."
    )


def test_clean_rejects_empty_output():
    assert clean_generated_body(None, fact=FACT, budget=200) is None
    assert clean_generated_body("   ", fact=FACT, budget=200) is None


def test_clean_rejects_links_mentions_hashtags_and_tickers():
    for bad in (
        "Cash Bears live https://evil.example",
        "Cash Bears live www.evil.example",
        "Cash Bears live thanks @someone",
        "Cash Bears live #gem",
        "Cash Bears live $BEAR",
    ):
        assert clean_generated_body(bad, fact=FACT, budget=200) is None


def test_clean_rejects_numbers_that_are_not_in_the_facts():
    # Модель выдумала цену — такое владельцу показывать незачем.
    assert clean_generated_body("Cash Bears mints at 0.05 ETH", fact=FACT, budget=200) is None
    assert clean_generated_body("Cash Bears: 999 wallets in", fact=FACT, budget=200) is None


def test_clean_rejects_a_body_over_the_budget():
    assert clean_generated_body("Cash Bears " * 30, fact=FACT, budget=50) is None


def test_clean_accepts_a_comma_formatted_number_matching_the_fact():
    # "2,222" и "2222" — одно и то же число, просто с разделителем разрядов
    # (естественная манера письма для живой модели). Раньше это ошибочно
    # считалось выдумкой и откатывалось к шаблону без всякой причины.
    fact = "Mint confirmed: Cash Bears, max supply 2222"
    body = "Cash Bears mint is live, capped at 2,222 pieces."
    assert clean_generated_body(body, fact=fact, budget=200) == body


# --- порог "стоит ли постить/цитировать" (зашит в caption_prompt/quote_prompt) ---


def test_caption_prompt_offers_a_skip_option():
    system, _ = caption_prompt(FACT, 200)
    assert SKIP_TOKEN in system


def test_quote_prompt_offers_a_skip_option():
    system, _ = quote_prompt("free mint on robinhood chain")
    assert SKIP_TOKEN in system


def test_is_skip_reply_recognizes_the_token_loosely():
    assert is_skip_reply("SKIP") is True
    assert is_skip_reply("  skip  ") is True
    assert is_skip_reply(None) is False
    assert is_skip_reply("") is False
    assert is_skip_reply("Cash Bears is minting fast.") is False


# --- комментарий к чужому твиту ---


def test_quote_budget_reserves_room_for_the_attached_card():
    assert quote_budget() == MAX_LEN - 25


def test_quote_prompt_carries_the_post_text_and_the_budget():
    system, user = quote_prompt("free mint on robinhood chain")
    assert str(quote_budget()) in system
    assert "free mint on robinhood chain" in user
    assert "no hashtags" in system
    assert "@mentions" in system


def test_quote_comment_validation_rejects_an_invented_number():
    tweet = "free mint on robinhood chain, 4444 supply"
    bad = "Sounds fun, but I'd check the 0.05 ETH price first."
    assert clean_generated_body(bad, fact=tweet, budget=quote_budget()) is None


def test_quote_comment_validation_accepts_a_faithful_reaction():
    tweet = "free mint on robinhood chain, 4444 supply"
    good = "4444 is a big supply for a free mint — curious how fast this fills."
    assert clean_generated_body(good, fact=tweet, budget=quote_budget()) == good
