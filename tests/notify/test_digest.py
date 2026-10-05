import asyncio
from datetime import datetime, time, timedelta

from wlparser.notify.digest import (
    DEFAULT_DIGEST_TIMES,
    MAX_LOOKBACK_HOURS,
    attach_takes,
    digest_since,
    format_digest,
    next_digest_at,
    parse_times,
)

# --- parse_times ---


def test_parse_times_parses_hh_mm_strings():
    assert parse_times(["08:00", "13:00", "21:00"]) == (time(8, 0), time(13, 0), time(21, 0))


def test_parse_times_skips_bad_entries():
    assert parse_times(["08:00", "not a time", "21:00"]) == (time(8, 0), time(21, 0))


def test_parse_times_falls_back_to_default_when_everything_is_bad():
    assert parse_times(["garbage"]) == DEFAULT_DIGEST_TIMES


def test_default_digest_times_are_every_two_hours():
    assert DEFAULT_DIGEST_TIMES == tuple(time(h, 0) for h in range(0, 24, 2))


# --- next_digest_at ---

_TIMES = (time(8, 0), time(13, 0), time(21, 0))


def test_picks_the_next_time_later_today():
    now = datetime(2026, 1, 1, 10, 0)
    assert next_digest_at(now, _TIMES) == datetime(2026, 1, 1, 13, 0)


def test_picks_the_soonest_of_several_upcoming_times():
    now = datetime(2026, 1, 1, 6, 0)
    assert next_digest_at(now, _TIMES) == datetime(2026, 1, 1, 8, 0)


def test_wraps_to_tomorrow_when_all_of_today_has_passed():
    now = datetime(2026, 1, 1, 22, 0)
    assert next_digest_at(now, _TIMES) == datetime(2026, 1, 2, 8, 0)


def test_exactly_on_a_digest_time_waits_for_the_next_one():
    # Не шлём дайджест дважды подряд, если планировщик проснулся ровно в срок.
    now = datetime(2026, 1, 1, 8, 0)
    assert next_digest_at(now, _TIMES) == datetime(2026, 1, 1, 13, 0)


def test_custom_times_are_respected():
    now = datetime(2026, 1, 1, 0, 0)
    assert next_digest_at(now, (time(9, 30),)) == datetime(2026, 1, 1, 9, 30)


def test_default_two_hour_cadence_picks_the_next_even_hour():
    now = datetime(2026, 1, 1, 10, 15)
    assert next_digest_at(now, DEFAULT_DIGEST_TIMES) == datetime(2026, 1, 1, 12, 0)


# --- digest_since ---


def test_first_ever_digest_looks_back_the_max_window():
    now = datetime(2026, 1, 5, 8, 0)
    assert digest_since(None, now=now) == now - timedelta(hours=MAX_LOOKBACK_HOURS)


def test_recent_last_sent_is_used_as_is():
    now = datetime(2026, 1, 5, 13, 0)
    last_sent = (now - timedelta(hours=1)).isoformat()
    assert digest_since(last_sent, now=now) == now - timedelta(hours=1)


def test_stale_last_sent_is_clamped_to_the_max_window():
    # Бот не отправлял дайджест несколько дней — не зачерпываем всё разом.
    now = datetime(2026, 1, 5, 8, 0)
    last_sent = (now - timedelta(days=3)).isoformat()
    assert digest_since(last_sent, now=now) == now - timedelta(hours=MAX_LOOKBACK_HOURS)


def test_garbled_last_sent_falls_back_to_the_max_window():
    now = datetime(2026, 1, 5, 8, 0)
    assert digest_since("not a date", now=now) == now - timedelta(hours=MAX_LOOKBACK_HOURS)


# --- format_digest ---


def test_empty_digest_returns_none():
    assert format_digest([], []) is None


def test_digest_lists_projects_with_score_and_reason():
    projects = [
        {
            "address": "0x" + "a" * 40,
            "name": "Cash Bears",
            "interest_score": 80,
            "interest_reason": "флор выше минта",
            "chain": "robinhood",
        }
    ]
    text = format_digest(projects, [])
    assert "Cash Bears" in text
    assert "80" in text
    assert "флор выше минта" in text
    assert "opensea.io/assets/robinhood/" in text


def test_digest_falls_back_to_address_without_a_name():
    address = "0x" + "b" * 40
    projects = [{"address": address, "name": None, "interest_score": None}]
    text = format_digest(projects, [])
    assert address in text


def test_digest_lists_twitter_announcements_with_links():
    announcements = [{"text": "WL for our next drop", "url": "https://x.com/a/status/1"}]
    text = format_digest([], announcements)
    assert "WL for our next drop" in text
    assert 'href="https://x.com/a/status/1"' in text


def test_digest_shows_the_analytical_take_when_present():
    announcements = [
        {
            "text": "WL for our next drop",
            "url": "https://x.com/a/status/1",
            "take": "Reads like a templated farming post, light on specifics.",
        }
    ]
    text = format_digest([], announcements)
    assert "Reads like a templated farming post" in text


def test_digest_escapes_untrusted_text():
    projects = [{"address": "0x" + "c" * 40, "name": "<script>x</script>", "interest_score": 1}]
    text = format_digest(projects, [])
    assert "<script>" not in text


# --- attach_takes ---


class FakeLLM:
    def __init__(self, reply=None, *, raises=False, hang=False):
        self.reply = reply
        self.raises = raises
        self.hang = hang
        self.calls = []

    async def chat(self, system, user, **kwargs):
        self.calls.append((system, user))
        if self.hang:
            await asyncio.sleep(10)
        if self.raises:
            raise RuntimeError("model exploded")
        return self.reply


async def test_attach_takes_without_an_llm_leaves_rows_unchanged():
    rows = [{"text": "WL soon"}]
    assert await attach_takes(rows, None) == rows


async def test_attach_takes_adds_the_cleaned_reply():
    rows = [{"text": "WL soon"}]
    result = await attach_takes(rows, FakeLLM("  Looks templated, light on details.  "))
    assert result[0]["take"] == "Looks templated, light on details."


async def test_attach_takes_tolerates_the_model_raising():
    rows = [{"text": "WL soon"}]
    result = await attach_takes(rows, FakeLLM(raises=True))
    assert "take" not in result[0]


async def test_attach_takes_tolerates_a_slow_model():
    rows = [{"text": "WL soon"}]
    result = await attach_takes(rows, FakeLLM(hang=True), timeout_s=0.05)
    assert "take" not in result[0]


async def test_attach_takes_skips_an_empty_reply():
    rows = [{"text": "WL soon"}]
    result = await attach_takes(rows, FakeLLM(None))
    assert "take" not in result[0]
