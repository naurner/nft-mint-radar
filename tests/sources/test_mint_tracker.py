from datetime import datetime, timedelta, timezone

from wlparser.sources.mint_tracker import MintTracker

NFT = "0x" + "a" * 40
T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def wallet(n: int) -> str:
    return "0x" + format(n, "040x")


def test_single_mint_reports_nothing():
    # Ровно та ситуация, которая раньше давала ложный алерт.
    tracker = MintTracker(min_unique_minters=10)
    assert tracker.record(NFT, wallet(1), T0) is None


def test_reports_once_threshold_of_unique_wallets_is_reached():
    tracker = MintTracker(min_unique_minters=5)
    results = [tracker.record(NFT, wallet(i), T0) for i in range(1, 6)]
    assert all(r is None for r in results[:4])
    stats = results[4]
    assert stats is not None
    assert stats.unique_minters == 5
    assert stats.total_mints == 5


def test_same_wallet_minting_many_times_does_not_reach_threshold():
    # Один кошелёк, скупивший 50 штук, — это не раздача на публику.
    tracker = MintTracker(min_unique_minters=10)
    for _ in range(50):
        assert tracker.record(NFT, wallet(1), T0) is None


def test_reports_only_once_per_contract():
    tracker = MintTracker(min_unique_minters=3)
    for i in range(1, 4):
        result = tracker.record(NFT, wallet(i), T0)
    assert result is not None
    for i in range(4, 30):
        assert tracker.record(NFT, wallet(i), T0) is None


def test_total_mints_counts_every_mint_not_just_unique():
    tracker = MintTracker(min_unique_minters=3)
    tracker.record(NFT, wallet(1), T0)
    tracker.record(NFT, wallet(1), T0)
    tracker.record(NFT, wallet(2), T0)
    stats = tracker.record(NFT, wallet(3), T0)
    assert stats.unique_minters == 3
    assert stats.total_mints == 4


def test_slow_trickle_across_a_long_period_never_reports():
    # Служебный контракт набирает кошельки месяцами — окно каждый раз
    # истекает раньше, чем порог достигнут.
    tracker = MintTracker(min_unique_minters=5, window_minutes=60)
    for i in range(1, 40):
        ts = T0 + timedelta(hours=2 * i)
        assert tracker.record(NFT, wallet(i), ts) is None


def test_burst_inside_window_reports_even_after_earlier_slow_period():
    tracker = MintTracker(min_unique_minters=4, window_minutes=60)
    tracker.record(NFT, wallet(1), T0)
    tracker.record(NFT, wallet(2), T0 + timedelta(hours=5))

    later = T0 + timedelta(hours=10)
    assert tracker.record(NFT, wallet(3), later) is None
    assert tracker.record(NFT, wallet(4), later) is None
    assert tracker.record(NFT, wallet(5), later) is None
    stats = tracker.record(NFT, wallet(6), later)
    assert stats is not None
    assert stats.unique_minters == 4


def test_minutes_active_is_measured():
    tracker = MintTracker(min_unique_minters=2)
    tracker.record(NFT, wallet(1), T0)
    stats = tracker.record(NFT, wallet(2), T0 + timedelta(minutes=18))
    assert round(stats.minutes_active) == 18


def test_contracts_are_tracked_independently():
    other = "0x" + "b" * 40
    tracker = MintTracker(min_unique_minters=2)
    assert tracker.record(NFT, wallet(1), T0) is None
    assert tracker.record(other, wallet(2), T0) is None
    assert tracker.record(NFT, wallet(3), T0) is not None


def test_missing_minter_address_still_counts_total():
    tracker = MintTracker(min_unique_minters=2)
    assert tracker.record(NFT, None, T0) is None
    assert tracker.record(NFT, None, T0) is None


def test_eviction_keeps_dictionary_bounded():
    tracker = MintTracker(min_unique_minters=100, window_minutes=1, max_tracked=50)
    for i in range(400):
        tracker.record("0x" + format(i, "040x"), wallet(i), T0 + timedelta(minutes=i))
    assert len(tracker._entries) <= 50
