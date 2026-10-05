"""Проверки построены на реальном потоке, который забил чат владельца."""

from datetime import datetime, timedelta, timezone

from wlparser.models import Signal
from wlparser.pipeline.noise import NoiseFilter, normalize, similarity, topic_key

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
NFT = "0x" + "a" * 40

# Шаблон, который в реальной ленте опубликовали пять разных аккаунтов
# с аудиторией от 136 до 8866 подписчиков.
TEMPLATE = (
    "I'm on the HoodMiners whitelist. One chassis, one wallet, "
    "and the rig earns tokenized stock while it runs."
)


def tweet(text=TEMPLATE, author="someone", followers=5000, address=None):
    return Signal(
        source="twitter",
        kind="wl_announced",
        text=text,
        url="https://x.com/a/status/1",
        author=author,
        contract_address=address,
        chain=None,
        timestamp=T0,
        raw={"author_followers": followers},
    )


def onchain(kind="mint_started", address=NFT, text="Mint confirmed: Cash Bears"):
    return Signal(
        source="onchain",
        kind=kind,
        text=text,
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=T0,
        raw={},
    )


def make(**kwargs):
    kwargs.setdefault("max_alerts_per_hour", 100)
    kwargs.setdefault("topic_cooldown_minutes", 0)
    return NoiseFilter(**kwargs)


# --- шаблонные посты ---


def test_identical_template_from_many_accounts_is_reported_once():
    """Главный случай: один текст, пять авторов, аудитория от 136 до 8866."""
    f = make()
    assert f.allow(tweet(author="Rajsri7xx", followers=136), 50, T0).allowed
    for author, followers in [
        ("DiivaMira", 2173),
        ("anhm86178", 5243),
        ("masud2633", 6399),
        ("Network_Hustler", 8866),
    ]:
        verdict = f.allow(tweet(author=author, followers=followers), 50, T0)
        assert not verdict.allowed, author
        assert "шаблон" in verdict.reason


def test_template_with_swapped_links_and_mentions_is_still_caught():
    # В раздаваемых шаблонах меняют именно ссылки и упоминания.
    f = make()
    f.allow(tweet(text=TEMPLATE + " https://t.co/aaa @friend"), 50, T0)
    assert not f.allow(tweet(text=TEMPLATE + " https://t.co/zzz @other"), 50, T0).allowed


def test_different_announcements_both_pass():
    f = make()
    assert f.allow(tweet(text="BLOOMZ MINT IS LIVE, GTD mint is now open"), 50, T0).allowed
    assert f.allow(
        tweet(text="DEAD PIXELS MINT IS LIVE, the glitch is official"), 50, T0
    ).allowed


def test_template_is_forgotten_after_the_window():
    f = make(text_window_hours=12)
    f.allow(tweet(), 50, T0)
    assert f.allow(tweet(), 50, T0 + timedelta(hours=13)).allowed


def test_very_short_text_is_never_treated_as_a_template():
    f = make()
    assert f.allow(tweet(text="mint live"), 50, T0).allowed
    assert f.allow(tweet(text="mint live"), 50, T0).allowed


# --- один проект во множестве сообщений ---


def test_second_message_about_the_same_project_is_held_back():
    f = make(topic_cooldown_minutes=240)
    assert f.allow(tweet(text="great things about @hoodminers_rh soon"), 50, T0).allowed
    verdict = f.allow(tweet(text="another take on @hoodminers_rh today"), 50, T0)
    assert not verdict.allowed
    assert "недавно" in verdict.reason


def test_the_same_project_passes_again_after_the_cooldown():
    f = make(topic_cooldown_minutes=240)
    f.allow(tweet(text="news about @hoodminers_rh"), 50, T0)
    later = T0 + timedelta(hours=5)
    assert f.allow(tweet(text="fresh news about @hoodminers_rh"), 50, later).allowed


def test_different_projects_do_not_block_each_other():
    f = make(topic_cooldown_minutes=240)
    assert f.allow(tweet(text="mint from @hoodminers_rh"), 50, T0).allowed
    assert f.allow(tweet(text="mint from @shareHOODLers"), 50, T0).allowed


def test_contract_address_is_the_topic_when_present():
    assert topic_key(tweet(address=NFT)) == f"contract:{NFT}"


def test_mentions_become_the_topic_without_a_contract():
    assert topic_key(tweet(text="hey @alpha and @beta")) == "mentions:alpha,beta"


def test_message_about_nothing_specific_has_no_topic():
    assert topic_key(tweet(text="mint is live now")) is None


# --- размер аудитории ---


def test_tiny_account_is_filtered():
    f = make(min_author_followers=100)
    verdict = f.allow(tweet(author="0xagentchaos", followers=29), 50, T0)
    assert not verdict.allowed
    assert "29" in verdict.reason


def test_unknown_follower_count_is_not_punished():
    signal = tweet()
    signal.raw.pop("author_followers")
    assert make().allow(signal, 50, T0).allowed


# --- потолок в час ---


DISTINCT = [
    "BLOOMZ garden access is now open for holders",
    "DEAD PIXELS glitch collection dropping tonight",
    "Ticker Box public sale starts within the hour",
    "Psycho Hoods free claim available to everyone",
    "Broker Cats reveal happens tomorrow morning",
]


def test_hourly_cap_stops_a_flood():
    f = make(max_alerts_per_hour=3, topic_cooldown_minutes=0)
    for text in DISTINCT[:3]:
        assert f.allow(tweet(text=text), 50, T0).allowed, text
    verdict = f.allow(tweet(text=DISTINCT[3]), 50, T0)
    assert not verdict.allowed
    assert "лимит" in verdict.reason


def test_cap_frees_up_as_the_hour_passes():
    f = make(max_alerts_per_hour=2, topic_cooldown_minutes=0)
    f.allow(tweet(text=DISTINCT[0]), 50, T0)
    f.allow(tweet(text=DISTINCT[1]), 50, T0)
    later = T0 + timedelta(hours=1, minutes=1)
    assert f.allow(tweet(text=DISTINCT[2]), 50, later).allowed


def test_hot_finding_ignores_every_limit():
    # Всплеск минта должен доходить даже когда всё остальное придержано.
    f = make(max_alerts_per_hour=1, topic_cooldown_minutes=240, urgent_score=85)
    f.allow(tweet(text="Broker Cats reveal happens tomorrow morning"), 50, T0)
    assert f.allow(onchain(kind="mint_surge"), 95, T0).allowed


# --- он-чейн ---


def test_onchain_facts_skip_the_social_rules():
    # Он-чейн событие — факт, а не чужой текст; проверки для соцсетей к нему
    # неприменимы, даже если формулировка повторяется.
    f = make()
    assert f.allow(onchain(address="0x" + "b" * 40), 85, T0).allowed
    assert f.allow(onchain(address="0x" + "c" * 40), 85, T0).allowed


def test_onchain_still_counts_towards_the_hourly_cap():
    f = make(max_alerts_per_hour=1, urgent_score=200)
    assert f.allow(onchain(address="0x" + "b" * 40), 85, T0).allowed
    assert not f.allow(onchain(address="0x" + "c" * 40), 85, T0).allowed


def test_second_onchain_signal_for_the_same_contract_is_held_back():
    f = make()  # onchain_topic_cooldown_minutes по умолчанию — 30
    address = "0x" + "d" * 40
    assert f.allow(onchain(kind="mint_started", address=address), 85, T0).allowed
    verdict = f.allow(onchain(kind="mint_surge", address=address), 95, T0)
    assert not verdict.allowed


def test_near_sellout_bypasses_the_onchain_cooldown():
    # У быстрых минтов "почти распродано" может наступить в первые минуты
    # после старта — раньше, чем истечёт обычный 30-минутный кулдаун на
    # контракт. Он и так шлётся не больше раза за жизнь проекта (LiveTracker
    # это гарантирует отдельно), так что общий кулдаун ему просто не нужен.
    f = make()
    address = "0x" + "e" * 40
    assert f.allow(onchain(kind="mint_started", address=address), 85, T0).allowed
    verdict = f.allow(onchain(kind="mint_near_sellout", address=address), 95, T0)
    assert verdict.allowed


# --- вспомогательные функции ---


def test_normalize_drops_links_mentions_and_punctuation():
    words = normalize("Mint is LIVE!! https://t.co/x @someone 🔥")
    assert "mint" in words and "live" in words
    assert not any(w.startswith("http") for w in words)
    assert "someone" not in words


def test_similarity_of_identical_texts_is_one():
    assert similarity(normalize(TEMPLATE), normalize(TEMPLATE)) == 1.0


def test_similarity_of_unrelated_texts_is_low():
    a = normalize("BLOOMZ MINT IS LIVE, garden access open")
    b = normalize("Nucleus leaderboard goes live at noon UTC")
    assert similarity(a, b) < 0.2
