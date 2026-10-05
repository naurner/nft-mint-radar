"""Проверка пригодности RPC-эндпоинта.

Без работающего eth_getLogs мы не увидим минты, поэтому endpoint,
отвечающий только на eth_blockNumber, для нас бесполезен — и лучше
узнать об этом при настройке, а не через сутки тишины в Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    usable: bool
    chain_id: int | None = None
    block_number: int | None = None
    problem: str = ""


async def probe_endpoint(client) -> ProbeResult:
    try:
        chain_id = int(await client.call("eth_chainId"), 16)
    except Exception as exc:
        return ProbeResult(False, problem=f"не отвечает на eth_chainId: {exc}")

    try:
        block_number = int(await client.call("eth_blockNumber"), 16)
    except Exception as exc:
        return ProbeResult(
            False, chain_id=chain_id, problem=f"не отвечает на eth_blockNumber: {exc}"
        )

    try:
        await client.call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(max(block_number - 1, 0)),
                    "toBlock": hex(block_number),
                    "topics": [],
                }
            ],
        )
    except Exception as exc:
        return ProbeResult(
            False,
            chain_id=chain_id,
            block_number=block_number,
            problem=f"не поддерживает eth_getLogs, минты ловить нечем: {exc}",
        )

    return ProbeResult(True, chain_id=chain_id, block_number=block_number)
