"""compute_interest_score — эвристика, а не модель, но должна быть предсказуемой."""

from wlparser.tracking.scoring import compute_interest_score

PROJECT = {"address": "0x" + "a" * 40, "max_supply": 1000}


def snap(
    ts,
    *,
    total_supply=None,
    mint_price_wei=None,
    floor_price_eth=None,
    floor_price_symbol="ETH",
    unique_minters=None,
):
    return {
        "ts": ts,
        "total_supply": total_supply,
        "max_supply": PROJECT["max_supply"],
        "mint_price_wei": mint_price_wei,
        "floor_price_eth": floor_price_eth,
        "floor_price_symbol": floor_price_symbol,
        "unique_minters": unique_minters,
    }


def test_no_history_gives_zero_and_says_so():
    score, reason = compute_interest_score(PROJECT, [])
    assert score == 0
    assert "недостаточно" in reason


def test_almost_sold_out_scores_higher_than_half_sold():
    full = compute_interest_score(PROJECT, [snap("2026-01-01T00:00:00+00:00", total_supply=960)])
    half = compute_interest_score(PROJECT, [snap("2026-01-01T00:00:00+00:00", total_supply=600)])
    empty = compute_interest_score(PROJECT, [snap("2026-01-01T00:00:00+00:00", total_supply=10)])
    assert full[0] > half[0] > empty[0]


def test_free_mint_with_positive_floor_is_rewarded():
    score, reason = compute_interest_score(
        PROJECT,
        [snap("2026-01-01T00:00:00+00:00", mint_price_wei=0, floor_price_eth=0.02)],
    )
    assert score > 0
    assert "бесплатный" in reason


def test_floor_above_mint_price_scores_higher_than_floor_below():
    mint_wei = 10**16  # 0.01 ETH
    above = compute_interest_score(
        PROJECT, [snap("t", mint_price_wei=mint_wei, floor_price_eth=0.02)]
    )
    below = compute_interest_score(
        PROJECT, [snap("t", mint_price_wei=mint_wei, floor_price_eth=0.003)]
    )
    assert above[0] > below[0]
    assert "выше цены минта" in above[1]
    assert "ниже цены минта" in below[1]


def test_dumped_floor_can_pull_score_negative_before_clamping():
    # Одна только просевшая цена не должна давать высокий балл, даже с fill=0.
    mint_wei = 10**16
    score, reason = compute_interest_score(
        {"address": PROJECT["address"], "max_supply": 1000},
        [snap("t", total_supply=0, mint_price_wei=mint_wei, floor_price_eth=0.001)],
    )
    assert score == 0  # клампится снизу, а не уходит в минус
    assert "ниже цены минта" in reason


def test_floor_in_a_different_currency_is_not_compared_to_mint_price():
    # Живой случай: флор в USDG (стейблкоин), цена минта — в ETH. Делить
    # одно на другое значило бы сравнивать несравнимое, поэтому этот
    # множитель просто пропускается, а не считается вслепую.
    mint_wei = 10**16
    score, reason = compute_interest_score(
        PROJECT,
        [snap("t", mint_price_wei=mint_wei, floor_price_eth=100.0, floor_price_symbol="USDG")],
    )
    assert "цены минта" not in reason


def test_missing_floor_symbol_is_treated_as_unknown_not_eth():
    # Старые снимки, записанные до того, как валюта флора учитывалась,
    # не имеют этого поля вовсе — они не должны молча считаться ETH.
    mint_wei = 10**16
    score, reason = compute_interest_score(
        PROJECT,
        [snap("t", mint_price_wei=mint_wei, floor_price_eth=0.02, floor_price_symbol=None)],
    )
    assert "цены минта" not in reason


def test_fast_holder_growth_is_rewarded():
    history = [
        snap("2026-01-01T00:00:00+00:00", unique_minters=10),
        snap("2026-01-01T01:00:00+00:00", unique_minters=40),
    ]
    fast_score, fast_reason = compute_interest_score(PROJECT, history)

    slow_history = [
        snap("2026-01-01T00:00:00+00:00", unique_minters=10),
        snap("2026-01-01T01:00:00+00:00", unique_minters=12),
    ]
    slow_score, _ = compute_interest_score(PROJECT, slow_history)

    assert fast_score > slow_score
    assert "держателей в час" in fast_reason


def test_score_never_exceeds_bounds():
    history = [
        snap("2026-01-01T00:00:00+00:00", total_supply=100, unique_minters=0),
        snap(
            "2026-01-01T01:00:00+00:00",
            total_supply=1000,
            mint_price_wei=0,
            floor_price_eth=1.0,
            unique_minters=500,
        ),
    ]
    score, _ = compute_interest_score(PROJECT, history)
    assert 0 <= score <= 100


def test_missing_pieces_do_not_crash():
    # Свежий проект без цены и без флора — оценка просто ниже, не падает.
    score, reason = compute_interest_score(PROJECT, [snap("2026-01-01T00:00:00+00:00")])
    assert score == 0
    assert reason == "мало сигналов пока"
