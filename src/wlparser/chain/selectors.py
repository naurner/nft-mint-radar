"""Селекторы функций и топики событий EVM.

Значения вычисляются из строк сигнатур через keccak, а не хардкодятся:
опечатка в 64-символьном хеше не ловится глазами, а расхождение с
эталонными значениями в тестах ловится сразу.
"""

from eth_utils import keccak

ZERO_ADDRESS = "0x" + "0" * 40
ZERO_TOPIC = "0x" + "0" * 64


def function_selector(signature: str) -> str:
    """Первые 4 байта keccak от сигнатуры: 'name()' -> '0x06fdde03'."""
    return "0x" + keccak(text=signature).hex()[:8]


def event_topic(signature: str) -> str:
    """Полный keccak от сигнатуры события — topic0 в логах."""
    return "0x" + keccak(text=signature).hex()


# --- Селекторы функций чтения метаданных ---
SEL_NAME = function_selector("name()")
SEL_SYMBOL = function_selector("symbol()")
SEL_TOTAL_SUPPLY = function_selector("totalSupply()")
SEL_SUPPORTS_INTERFACE = function_selector("supportsInterface(bytes4)")

# Цена и лимит минта не стандартизированы — перебираем частые варианты.
MINT_PRICE_SIGNATURES = (
    "mintPrice()",
    "price()",
    "cost()",
    "publicPrice()",
    "PRICE()",
    "MINT_PRICE()",
    "publicSalePrice()",
)
MAX_SUPPLY_SIGNATURES = (
    "maxSupply()",
    "MAX_SUPPLY()",
    "collectionSize()",
    "totalSupplyCap()",
)

# --- Топики событий ---
TRANSFER_TOPIC = event_topic("Transfer(address,address,uint256)")
TRANSFER_SINGLE_TOPIC = event_topic(
    "TransferSingle(address,address,address,uint256,uint256)"
)
TRANSFER_BATCH_TOPIC = event_topic(
    "TransferBatch(address,address,address,uint256[],uint256[])"
)

# --- ERC-165 interface id: константы стандартов, а не производные значения ---
ERC165_INTERFACE_ID = "0x01ffc9a7"
ERC721_INTERFACE_ID = "0x80ac58cd"
ERC721_METADATA_INTERFACE_ID = "0x5b5e139f"
ERC1155_INTERFACE_ID = "0xd9b67a26"
