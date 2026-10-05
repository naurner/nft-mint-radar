from datetime import datetime, timezone

from wlparser.models import Signal


def make(**kwargs):
    base = dict(
        source="onchain",
        kind="contract_deployed",
        text="t",
        url=None,
        author=None,
        contract_address="0xAbC0000000000000000000000000000000000001",
        chain="robinhood",
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw={},
    )
    return Signal(**{**base, **kwargs})


def test_contract_address_is_lowercased():
    assert make().contract_address == "0xabc0000000000000000000000000000000000001"


def test_dedupe_key_uses_contract_when_present():
    assert make().dedupe_key == (
        "onchain:contract_deployed:0xabc0000000000000000000000000000000000001"
    )


def test_dedupe_key_falls_back_to_text_hash_without_contract():
    # Перепост того же объявления не должен давать второй алерт.
    a = make(contract_address=None, text="WL is live now")
    b = make(contract_address=None, text="wl   IS Live   now!")
    assert a.dedupe_key == b.dedupe_key


def test_different_text_gives_different_key():
    a = make(contract_address=None, text="WL is live")
    b = make(contract_address=None, text="mint sold out")
    assert a.dedupe_key != b.dedupe_key


def test_same_contract_different_kind_gives_different_key():
    # Деплой и старт минта — разные события одного проекта, оба важны.
    assert make(kind="contract_deployed").dedupe_key != make(
        kind="mint_started"
    ).dedupe_key


# --- format_amount ---


def test_format_amount_avoids_scientific_notation():
    from wlparser.models import format_amount

    # Живой пример: флор 4.1e-05 в научной записи никто не прочитает.
    assert format_amount(0.000041) == "0.000041"


def test_format_amount_strips_trailing_zeros():
    from wlparser.models import format_amount

    assert format_amount(0.01) == "0.01"
    assert format_amount(1.0) == "1"


def test_format_amount_keeps_meaningful_small_digits():
    from wlparser.models import format_amount

    assert format_amount(1.50133e-05) == "0.0000150133"


def test_format_amount_zero():
    from wlparser.models import format_amount

    assert format_amount(0.0) == "0"


# --- looks_like_junk_contract ---


def test_no_name_and_no_symbol_is_junk():
    from wlparser.models import looks_like_junk_contract

    assert looks_like_junk_contract(None, None, 500) is True


def test_a_named_collection_with_a_normal_supply_is_not_junk():
    from wlparser.models import looks_like_junk_contract

    assert looks_like_junk_contract("Cash Bears", "CB", 2222) is False


def test_absurd_total_supply_is_junk_even_with_a_name():
    # Живой пример: карточка с именем-адресом и саплаем 1e28 — явно не NFT.
    from wlparser.models import looks_like_junk_contract

    assert looks_like_junk_contract("Some Token", "ST", 10**28) is True


def test_unknown_total_supply_is_not_junk_by_itself():
    from wlparser.models import looks_like_junk_contract

    assert looks_like_junk_contract("Cash Bears", "CB", None) is False
