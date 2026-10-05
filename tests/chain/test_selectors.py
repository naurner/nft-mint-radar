"""Сверка вычисленных значений с общеизвестными эталонами.

Если наша функция воспроизводит канонические селекторы Ethereum,
значит keccak и обрезка байтов работают верно.
"""

from wlparser.chain.selectors import (
    ERC1155_INTERFACE_ID,
    ERC165_INTERFACE_ID,
    ERC721_INTERFACE_ID,
    TRANSFER_SINGLE_TOPIC,
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    ZERO_TOPIC,
    function_selector,
)


def test_known_function_selectors():
    assert function_selector("name()") == "0x06fdde03"
    assert function_selector("symbol()") == "0x95d89b41"
    assert function_selector("totalSupply()") == "0x18160ddd"
    assert function_selector("supportsInterface(bytes4)") == "0x01ffc9a7"


def test_known_event_topics():
    assert TRANSFER_TOPIC == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    assert TRANSFER_SINGLE_TOPIC == (
        "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
    )


def test_known_interface_ids():
    assert ERC165_INTERFACE_ID == "0x01ffc9a7"
    assert ERC721_INTERFACE_ID == "0x80ac58cd"
    assert ERC1155_INTERFACE_ID == "0xd9b67a26"


def test_zero_constants_have_right_width():
    assert ZERO_TOPIC == "0x" + "0" * 64
    assert ZERO_ADDRESS == "0x" + "0" * 40
