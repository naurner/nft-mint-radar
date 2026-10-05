import asyncio
from datetime import datetime, timezone

from wlparser.models import Signal
from wlparser.quality import SignalQualityFilter, quality_prompt

ADDRESS = "0x" + "a" * 40


def make_signal(text="Cash Bears mint is live", kind="mint_started"):
    return Signal(
        source="onchain",
        kind=kind,
        text=text,
        url=None,
        author=None,
        contract_address=ADDRESS,
        chain="robinhood",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        raw={},
    )


class FakeLLM:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    async def chat(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


class HangingLLM:
    async def chat(self, system, user, **kwargs):
        await asyncio.sleep(10)
        return "NO"


def test_prompt_includes_kind_and_text():
    system, user = quality_prompt("mint_started", "Cash Bears mint is live")
    assert "YES" in system and "NO" in system
    assert "mint_started" in user
    assert "Cash Bears mint is live" in user


async def test_no_llm_configured_always_allows():
    filt = SignalQualityFilter(None)
    assert await filt.allow(make_signal()) is True


async def test_explicit_no_suppresses_the_signal():
    llm = FakeLLM("NO")
    filt = SignalQualityFilter(llm)
    assert await filt.allow(make_signal()) is False
    assert llm.calls[0][2]["max_tokens"] == 5
    assert llm.calls[0][2]["temperature"] == 0


async def test_yes_allows_the_signal():
    llm = FakeLLM("YES")
    filt = SignalQualityFilter(llm)
    assert await filt.allow(make_signal()) is True


async def test_ambiguous_reply_fails_open():
    llm = FakeLLM("hmm, hard to say")
    filt = SignalQualityFilter(llm)
    assert await filt.allow(make_signal()) is True


async def test_empty_reply_fails_open():
    llm = FakeLLM(None)
    filt = SignalQualityFilter(llm)
    assert await filt.allow(make_signal()) is True


async def test_llm_error_fails_open():
    llm = FakeLLM(RuntimeError("boom"))
    filt = SignalQualityFilter(llm)
    assert await filt.allow(make_signal()) is True


async def test_slow_llm_fails_open_within_timeout():
    filt = SignalQualityFilter(HangingLLM(), timeout_s=0.05)
    assert await filt.allow(make_signal()) is True


async def test_lowercase_no_is_still_treated_as_no():
    llm = FakeLLM("no.")
    filt = SignalQualityFilter(llm)
    assert await filt.allow(make_signal()) is False
