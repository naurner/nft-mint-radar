from datetime import datetime, timezone

from wlparser.db import Database
from wlparser.models import Signal
from wlparser.poster.caption import SKIP_TOKEN
from wlparser.poster.service import Poster

NFT = "0x" + "a" * 40


def onchain_signal(kind="mint_surge", address=NFT, text="Mint confirmed: Cash Bears"):
    return Signal(
        source="onchain",
        kind=kind,
        text=text,
        url=None,
        author=None,
        contract_address=address,
        chain="robinhood",
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw={},
    )


def tweet_signal(text="great WL coming!"):
    return Signal(
        source="twitter",
        kind="wl_announced",
        text=text,
        url="https://x.com/a/status/1",
        author="someone",
        contract_address=None,
        chain=None,
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw={},
    )


class FakeTwitter:
    def __init__(self, *, url="https://x.com/bot/status/999", fail=False):
        self.url = url
        self.fail = fail
        self.posted = []
        self.quoted = []

    async def post(self, text):
        if self.fail:
            raise RuntimeError("twitter down")
        self.posted.append(text)
        return self.url

    async def quote(self, text, tweet_url):
        if self.fail:
            raise RuntimeError("twitter down")
        self.quoted.append((text, tweet_url))
        return self.url


class OwnerNotifications:
    def __init__(self):
        self.drafts = []

    async def __call__(self, post_id, text):
        self.drafts.append((post_id, text))


async def make_db(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.connect()
    return db


async def test_onchain_signal_publishes_immediately_without_asking(tmp_path):
    # Владелец попросил не трогать его в Telegram на автопостинг — находка
    # должна уйти в X сама, без черновика и без подтверждения.
    db = await make_db(tmp_path)
    notify = OwnerNotifications()
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=notify)

    await poster.maybe_draft(onchain_signal())

    assert notify.drafts == []
    assert len(twitter.posted) == 1
    assert "Cash Bears" in twitter.posted[0]
    post = await db.get_post(1)  # первая и единственная запись в свежей базе
    assert post["status"] == "posted"
    await db.close()


async def test_twitter_sourced_signal_never_posts_anything(tmp_path):
    db = await make_db(tmp_path)
    notify = OwnerNotifications()
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=notify)

    await poster.maybe_draft(tweet_signal())

    assert notify.drafts == []
    assert twitter.posted == []
    await db.close()


async def test_approve_posts_to_twitter_and_marks_the_draft(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter(url="https://x.com/bot/status/42")
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications())

    post_id = await db.create_pending_post(address=NFT, text="draft")
    error = await poster.approve(post_id)

    assert error is None
    assert twitter.posted == ["draft"]
    post = await db.get_post(post_id)
    assert post["status"] == "posted"
    assert post["tweet_url"] == "https://x.com/bot/status/42"
    await db.close()


async def test_approve_twice_only_posts_once(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications())

    post_id = await db.create_pending_post(address=NFT, text="draft")
    await poster.approve(post_id)
    error = await poster.approve(post_id)

    assert error is not None
    assert len(twitter.posted) == 1  # второе нажатие не постит ещё раз
    await db.close()


async def test_skip_marks_the_draft_without_posting(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications())

    post_id = await db.create_pending_post(address=NFT, text="draft")
    await poster.skip(post_id)

    assert twitter.posted == []
    assert (await db.get_post(post_id))["status"] == "skipped"
    await db.close()


async def test_approve_reports_a_friendly_error_when_twitter_fails(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter(fail=True)
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications())

    post_id = await db.create_pending_post(address=NFT, text="draft")
    error = await poster.approve(post_id)

    assert error is not None
    assert (await db.get_post(post_id))["status"] == "pending"  # можно попробовать ещё раз
    await db.close()


async def test_no_limit_by_default_even_after_many_posts(tmp_path):
    # max_posts_per_day=None — владелец попросил убрать ограничение вообще.
    db = await make_db(tmp_path)
    notify = OwnerNotifications()
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=notify)

    for i in range(10):
        posted = await db.create_pending_post(address=NFT, text=f"old {i}")
        await db.decide_post(posted, status="posted", tweet_url=f"https://x.com/x/status/{i}")

    await poster.maybe_draft(onchain_signal(address="0x" + "c" * 40))

    assert len(twitter.posted) == 1
    await db.close()


async def test_daily_quota_blocks_auto_publishing(tmp_path):
    db = await make_db(tmp_path)
    notify = OwnerNotifications()
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=notify, max_posts_per_day=1)

    already_posted = await db.create_pending_post(address=NFT, text="old")
    await db.decide_post(already_posted, status="posted", tweet_url="https://x.com/x/status/1")

    await poster.maybe_draft(onchain_signal())

    assert notify.drafts == []
    assert twitter.posted == []  # лимит уже исчерпан, в X ничего не ушло
    await db.close()


async def test_daily_quota_blocks_approval_too(tmp_path):
    # Черновик мог быть создан до того, как лимит исчерпался чем-то ещё.
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), max_posts_per_day=1)

    already_posted = await db.create_pending_post(address=NFT, text="old")
    await db.decide_post(already_posted, status="posted", tweet_url="https://x.com/x/status/1")
    pending = await db.create_pending_post(address=NFT, text="new draft")

    error = await poster.approve(pending)

    assert error is not None
    assert twitter.posted == []
    assert (await db.get_post(pending))["status"] == "skipped"
    await db.close()


# --- локальная модель ---


class FakeLLM:
    """reply — то, что модель "написала бы" одним запросом (решение "стоит

    ли" зашито в caption_prompt/quote_prompt, не в отдельный вызов, так
    что этот дубль отвечает только текстом, без ветвления по max_tokens).
    """

    def __init__(self, reply=None, *, raises=False):
        self.reply = reply
        self.raises = raises
        self.calls = []

    async def chat(self, system, user, *, max_tokens=200, temperature=0.7):
        self.calls.append((system, user))
        if self.raises:
            raise RuntimeError("model exploded")
        return self.reply


async def test_published_text_uses_the_model_text_when_it_passes_validation(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    llm = FakeLLM("Cash Bears is minting — 25 wallets already.")
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=llm)

    await poster.maybe_draft(
        onchain_signal(text="Mint confirmed: Cash Bears, 25 wallets, 30 minted in ~3 min")
    )

    text = twitter.posted[0]
    assert text.startswith("Cash Bears is minting — 25 wallets already.")
    assert "https://opensea.io/assets/robinhood/" in text  # хвост собирает код
    assert "#RobinhoodChain" in text
    await db.close()


async def test_published_text_falls_back_to_the_template_when_the_model_is_silent(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM(None))

    await poster.maybe_draft(onchain_signal(text="Mint confirmed: Cash Bears"))

    assert twitter.posted[0].startswith("Mint confirmed: Cash Bears")
    await db.close()


async def test_published_text_falls_back_when_the_model_invents_a_link_or_a_price(tmp_path):
    for bad in ("Mint at 0.05 ETH now", "Check https://evil.example"):
        db = await make_db(tmp_path / bad.split()[0])
        twitter = FakeTwitter()
        poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM(bad))

        await poster.maybe_draft(onchain_signal(text="Mint confirmed: Cash Bears"))

        text = twitter.posted[0]
        assert text.startswith("Mint confirmed: Cash Bears")
        assert "evil.example" not in text
        assert "0.05" not in text
        await db.close()


async def test_publishing_is_skipped_entirely_when_the_model_errors(tmp_path):
    # Решение "стоит ли" и текст теперь один запрос — если модель упала
    # или не ответила вовремя, неизвестно и то, стоит ли вообще публиковать,
    # не только как. Без счётного лимита молчать дешевле, чем угадывать.
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM(raises=True))

    await poster.maybe_draft(onchain_signal(text="Mint confirmed: Cash Bears"))

    assert twitter.posted == []
    await db.close()


async def test_model_never_sees_twitter_sourced_signals(tmp_path):
    db = await make_db(tmp_path)
    llm = FakeLLM("anything")
    poster = Poster(db, twitter=FakeTwitter(), notify_owner=OwnerNotifications(), llm=llm)

    await poster.maybe_draft(tweet_signal())

    assert llm.calls == []
    await db.close()


async def test_skipped_and_pending_old_rows_do_not_block_new_publishing(tmp_path):
    # Лимит — на ОПУБЛИКОВАННОЕ, не на число предложений: старые
    # пропущенные/нерассмотренные записи не должны мешать публикации
    # новой находки, пока реально опубликованных меньше лимита.
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), max_posts_per_day=2)

    already = await db.create_pending_post(address=NFT, text="old")
    await db.decide_post(already, status="skipped")  # ни разу не опубликовано
    await db.create_pending_post(address=NFT, text="another")  # тоже pending

    await poster.maybe_draft(onchain_signal(address="0x" + "c" * 40))

    assert len(twitter.posted) == 1
    await db.close()


# --- send_test_draft ---


async def test_send_test_draft_ignores_the_daily_quota(tmp_path):
    # Ручная проверка публикации нужна именно тогда, когда лимит уже занят
    # старыми черновиками, которые не опубликовались — иначе её нечем
    # проверить до завтра.
    db = await make_db(tmp_path)
    notify = OwnerNotifications()
    poster = Poster(db, twitter=FakeTwitter(), notify_owner=notify, max_posts_per_day=1)
    await db.create_pending_post(address=NFT, text="old")  # лимит уже занят

    post_id = await poster.send_test_draft("проверка связи")

    assert len(notify.drafts) == 1
    assert notify.drafts[0] == (post_id, "проверка связи")
    post = await db.get_post(post_id)
    assert post["status"] == "pending"
    assert post["address"] is None
    await db.close()


async def test_send_test_draft_without_text_uses_a_timestamped_default(tmp_path):
    # X отклоняет повторную публикацию буквально того же текста как
    # дубликат — постоянная строка-заглушка ломала бы вторую проверку
    # подряд, так что дефолт содержит метку времени, а не фиксированный текст.
    db = await make_db(tmp_path)
    poster = Poster(db, twitter=FakeTwitter(), notify_owner=OwnerNotifications())

    post_id = await poster.send_test_draft()

    text = (await db.get_post(post_id))["text"]
    assert "UTC" in text
    await db.close()


async def test_approve_explains_a_twikit_duplicate_error(tmp_path):
    from twikit.errors import DuplicateTweet

    class DuplicateTwitter(FakeTwitter):
        async def post(self, text):
            raise DuplicateTweet("Status is a duplicate. (187)")

    db = await make_db(tmp_path)
    poster = Poster(db, twitter=DuplicateTwitter(), notify_owner=OwnerNotifications())
    post_id = await poster.send_test_draft("одинаковый текст")

    error = await poster.approve(post_id)

    assert error is not None
    assert "duplicate" in error.lower()
    await db.close()


async def test_send_test_draft_can_be_approved_like_any_other(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications())

    post_id = await poster.send_test_draft("проверка связи")
    error = await poster.approve(post_id)

    assert error is None
    assert twitter.posted == ["проверка связи"]
    await db.close()


# --- порог "стоит ли постить" (зашит в тот же запрос, что пишет текст) ---


async def test_worth_posting_skip_blocks_publishing(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM(SKIP_TOKEN))

    await poster.maybe_draft(onchain_signal())

    assert twitter.posted == []
    await db.close()


async def test_worth_posting_non_skip_text_allows_publishing(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db,
        twitter=twitter,
        notify_owner=OwnerNotifications(),
        llm=FakeLLM("Solid pace, worth a mention."),
    )

    await poster.maybe_draft(onchain_signal())

    assert len(twitter.posted) == 1
    await db.close()


async def test_worth_posting_gate_fails_closed_when_the_model_errors(tmp_path):
    # Без счётного лимита пропустить находку дешевле, чем опубликовать
    # посредственную — любая неопределённость трактуется как "нет".
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM(raises=True)
    )

    await poster.maybe_draft(onchain_signal())

    assert twitter.posted == []
    await db.close()


async def test_worth_posting_gate_is_a_noop_without_an_llm_at_all(tmp_path):
    # ИИ выключен в настройках целиком — порог неприменим, как до его
    # появления: не блокируем находку, когда нечем её оценить.
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=None)

    await poster.maybe_draft(onchain_signal())

    assert len(twitter.posted) == 1
    await db.close()


async def test_no_posts_quota_by_default(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db,
        twitter=twitter,
        notify_owner=OwnerNotifications(),
        llm=FakeLLM("Solid pace, worth a mention."),
    )

    for i in range(10):
        await poster.maybe_draft(onchain_signal(address="0x" + str(i) * 39 + "a"))

    assert len(twitter.posted) == 10  # без max_posts_per_day счётного предела нет
    await db.close()


# --- цитирование чужих твитов ---


async def test_maybe_quote_ignores_onchain_signals(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM("a comment")
    )

    await poster.maybe_quote(onchain_signal())

    assert twitter.quoted == []
    await db.close()


async def test_maybe_quote_ignores_chatter(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM("a comment")
    )

    sig = Signal(
        source="twitter",
        kind="chatter",
        text="gm",
        url="https://x.com/a/status/1",
        author="someone",
        contract_address=None,
        chain=None,
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw={},
    )
    await poster.maybe_quote(sig)

    assert twitter.quoted == []
    await db.close()


async def test_maybe_quote_ignores_a_signal_without_a_url(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM("a comment")
    )

    sig = Signal(
        source="twitter",
        kind="wl_announced",
        text="great WL coming!",
        url=None,
        author="someone",
        contract_address=None,
        chain=None,
        timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
        raw={},
    )
    await poster.maybe_quote(sig)

    assert twitter.quoted == []
    await db.close()


async def test_maybe_quote_skips_when_not_worth_it(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=FakeLLM(SKIP_TOKEN))

    await poster.maybe_quote(tweet_signal())

    assert twitter.quoted == []
    await db.close()


async def test_maybe_quote_without_an_llm_never_quotes(tmp_path):
    # Без ИИ нет шаблонного запасного текста для комментария — молчим.
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(db, twitter=twitter, notify_owner=OwnerNotifications(), llm=None)

    await poster.maybe_quote(tweet_signal())

    assert twitter.quoted == []
    await db.close()


async def test_maybe_quote_posts_a_validated_comment(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db,
        twitter=twitter,
        notify_owner=OwnerNotifications(),
        llm=FakeLLM("That's a big supply for a free mint, curious how fast it fills."),
    )

    await poster.maybe_quote(tweet_signal(text="great WL coming, 4444 supply!"))

    assert len(twitter.quoted) == 1
    text, quoted_url = twitter.quoted[0]
    assert text == "That's a big supply for a free mint, curious how fast it fills."
    assert quoted_url == "https://x.com/a/status/1"
    await db.close()


async def test_maybe_quote_stays_silent_when_the_comment_invents_a_number(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db,
        twitter=twitter,
        notify_owner=OwnerNotifications(),
        llm=FakeLLM("Sounds fun, but I'd check the 0.05 ETH price first."),
    )

    await poster.maybe_quote(tweet_signal(text="great WL coming!"))

    assert twitter.quoted == []
    await db.close()


async def test_quotes_have_their_own_quota_separate_from_posts(tmp_path):
    db = await make_db(tmp_path)
    twitter = FakeTwitter()
    poster = Poster(
        db,
        twitter=twitter,
        notify_owner=OwnerNotifications(),
        max_quotes_per_day=1,
        llm=FakeLLM("Curious how fast this one fills."),
    )

    await poster.maybe_quote(tweet_signal())
    await poster.maybe_quote(tweet_signal())

    assert len(twitter.quoted) == 1  # вторая цитата упёрлась в квоту цитирований

    # А обычный пост квоту цитирований не делит — свой лимит не занят.
    poster_for_posts = Poster(
        db,
        twitter=twitter,
        notify_owner=OwnerNotifications(),
        llm=FakeLLM("Solid pace, worth a mention."),
    )
    await poster_for_posts.maybe_draft(onchain_signal())
    assert len(twitter.posted) == 1
    await db.close()
