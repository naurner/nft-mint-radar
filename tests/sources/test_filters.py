from wlparser.chain.contracts import ContractInfo
from wlparser.sources.filters import ContractFilter


def info(name=None, symbol=None, address="0x" + "a" * 40, total_supply=None):
    return ContractInfo(
        address=address, standard="erc721", name=name, symbol=symbol,
        total_supply=total_supply,
    )


def test_uniswap_positions_are_ignored():
    # Ровно тот контракт, который засорял ленту в первом прогоне.
    assert ContractFilter().is_ignored(info(name="Uniswap v4 Positions NFT")) is True


def test_infrastructure_nfts_are_ignored():
    for name in [
        "Uniswap V3 Positions NFT-V1",
        "Aave Interest Bearing",
        "Staked ETH Vault",
        "Wrapped Punks",
        "ENS Name Service",
    ]:
        assert ContractFilter().is_ignored(info(name=name)) is True, name


def test_ordinary_collection_passes():
    assert ContractFilter().is_ignored(info(name="Cash Bears", symbol="CBRS")) is False
    assert ContractFilter().is_ignored(info(name="Onchain Harvest")) is False


def test_nameless_contract_is_ignored():
    # Без имени и символа это либо служебный контракт, либо недоделка.
    assert ContractFilter().is_ignored(info()) is True


def test_absurd_total_supply_is_ignored_even_with_a_name():
    # Живой пример: карточка с именем-адресом и саплаем ~1e28 — контракт,
    # на котором supportsInterface(ERC721) дал ложное "да" (скорее всего
    # ERC-20), а не настоящая коллекция.
    assert ContractFilter().is_ignored(
        info(name="Some Token", symbol="ST", total_supply=10**28)
    ) is True


def test_normal_supply_is_not_ignored():
    assert ContractFilter().is_ignored(
        info(name="Cash Bears", symbol="CBRS", total_supply=2222)
    ) is False


def test_explicit_address_blocklist():
    address = "0x" + "c" * 40
    filt = ContractFilter(ignore_addresses=[address.upper()])
    assert filt.is_ignored(info(name="Cash Bears", address=address)) is True


def test_custom_patterns_replace_defaults():
    filt = ContractFilter(ignore_name_patterns=["bears"])
    assert filt.is_ignored(info(name="Cash Bears")) is True
    # Uniswap больше не в списке, раз владелец задал свой.
    assert filt.is_ignored(info(name="Uniswap Positions")) is False


def test_symbol_is_also_checked():
    assert ContractFilter().is_ignored(info(name="Something", symbol="UNI-V3-POS")) is True
