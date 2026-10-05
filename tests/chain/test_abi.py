import pytest

from wlparser.chain.abi import decode_bool, decode_string, decode_uint, encode_bytes4_arg


def word(value: int) -> str:
    return format(value, "064x")


def test_encode_bytes4_arg_right_pads_to_32_bytes():
    assert encode_bytes4_arg("0x80ac58cd") == "80ac58cd" + "0" * 56


def test_encode_bytes4_arg_accepts_value_without_prefix():
    assert encode_bytes4_arg("80ac58cd") == "80ac58cd" + "0" * 56


def test_decode_bool_true_and_false():
    assert decode_bool("0x" + "0" * 63 + "1") is True
    assert decode_bool("0x" + "0" * 64) is False


def test_decode_uint():
    assert decode_uint("0x" + "0" * 62 + "ff") == 255


def test_decode_string_dynamic_encoding():
    payload = "0x" + word(32) + word(5) + b"Hello".hex().ljust(64, "0")
    assert decode_string(payload) == "Hello"


def test_decode_string_handles_bytes32_style_return():
    # Часть старых контрактов объявляет name() как bytes32, а не string.
    payload = "0x" + b"OCH".hex().ljust(64, "0")
    assert decode_string(payload) == "OCH"


@pytest.mark.parametrize("empty", ["0x", "", None])
def test_decoders_are_safe_on_empty_response(empty):
    assert decode_uint(empty) is None
    assert decode_string(empty) is None
    assert decode_bool(empty) is False


def test_decode_string_returns_none_on_garbage():
    assert decode_string("0xzzzz") is None
