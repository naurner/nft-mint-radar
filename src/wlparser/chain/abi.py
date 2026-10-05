"""Минимальное ABI-кодирование для сырых eth_call.

Полный ABI-кодер здесь избыточен: нам нужны вызовы без аргументов
и один bytes4, а из возвращаемых типов — bool, uint256 и string.
"""

from __future__ import annotations


def _strip(hexstr: str | None) -> str | None:
    if not hexstr:
        return None
    body = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return body or None


def encode_bytes4_arg(value: str) -> str:
    """bytes4 занимает левые 4 байта 32-байтового слова."""
    body = value[2:] if value.startswith("0x") else value
    return body.ljust(64, "0")


def decode_bool(hexstr: str | None) -> bool:
    body = _strip(hexstr)
    if body is None:
        return False
    try:
        return int(body[:64], 16) == 1
    except ValueError:
        return False


def decode_uint(hexstr: str | None) -> int | None:
    body = _strip(hexstr)
    if body is None:
        return None
    try:
        return int(body[:64], 16)
    except ValueError:
        return None


def decode_string(hexstr: str | None) -> str | None:
    """Декодирует и динамическую string, и bytes32-подобный ответ.

    Часть старых контрактов объявляет name() как bytes32, а не string,
    поэтому одного варианта декодирования недостаточно.
    """
    body = _strip(hexstr)
    if body is None:
        return None
    try:
        if len(body) >= 128:
            offset = int(body[:64], 16) * 2
            length = int(body[offset : offset + 64], 16) * 2
            raw = bytes.fromhex(body[offset + 64 : offset + 64 + length])
            return raw.decode("utf-8", errors="replace") or None
        raw = bytes.fromhex(body[:64]).rstrip(b"\x00")
        return raw.decode("utf-8", errors="replace") or None
    except (ValueError, IndexError):
        return None
