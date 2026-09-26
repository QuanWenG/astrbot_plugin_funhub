"""龟龟币公式：分量、连签封顶、7 / 30 天周期、补发口径。"""

from __future__ import annotations

import pytest

from funhub.coins import (
    backfill_reward_for_record,
    compute_reward,
    cycle_hits_for,
    expected_random_bonus,
    roll_random_bonus,
    streak_bonus_for,
)
from funhub.config import load_settings

COINS = load_settings({}).coins


def reward(streak: int, *, first: bool = False, random_bonus: int = 0):
    return compute_reward(
        COINS,
        streak_days=streak,
        is_first_checkin=first,
        random_bonus=random_bonus,
    )


def test_defaults_match_documented_numbers():
    assert COINS.base_reward == 100
    assert COINS.random_bonus_max == 50
    assert COINS.streak_bonus_per_day == 10
    assert COINS.streak_bonus_cap_days == 15
    assert COINS.cycle_short_days == 7
    assert COINS.cycle_short_bonus == 300
    assert COINS.cycle_long_days == 30
    assert COINS.cycle_long_bonus == 1500
    assert COINS.first_checkin_bonus == 200


def test_first_checkin_gets_welcome_bonus():
    result = reward(1, first=True)
    assert result.base == 100
    assert result.streak_bonus == 0
    assert result.cycle_hits == ()
    assert result.first_bonus == 200
    assert result.total == 300


def test_welcome_bonus_is_one_off():
    assert reward(1, first=False).total == 100


def test_streak_bonus_grows_then_caps():
    assert streak_bonus_for(COINS, 1) == 0
    assert streak_bonus_for(COINS, 2) == 10
    assert streak_bonus_for(COINS, 6) == 50
    assert streak_bonus_for(COINS, 16) == 150
    assert streak_bonus_for(COINS, 400) == 150


def test_short_cycle_repeats_every_seven_days():
    for day in (7, 14, 21, 28, 35):
        hits = cycle_hits_for(COINS, day)
        assert [hit.days for hit in hits] == [7], day
        assert hits[0].bonus == 300


def test_long_cycle_repeats_every_thirty_days():
    for day in (30, 60, 90):
        hits = cycle_hits_for(COINS, day)
        assert [hit.days for hit in hits] == [30], day
        assert hits[0].bonus == 1500


def test_plain_days_hit_nothing():
    for day in (0, 1, 6, 8, 13, 29, 31, 59):
        assert cycle_hits_for(COINS, day) == (), day


def test_two_hundred_and_tenth_day_hits_both_cycles():
    hits = cycle_hits_for(COINS, 210)
    assert [(hit.days, hit.bonus) for hit in hits] == [(7, 300), (30, 1500)]
    result = reward(210)
    assert result.cycle_bonus == 1800
    assert result.total == 100 + 0 + 150 + 1800


def test_zero_streak_only_pays_the_base():
    result = reward(0)
    assert result.streak_bonus == 0
    assert result.cycle_hits == ()
    assert result.total == 100


@pytest.mark.parametrize(
    ("streak", "total"),
    [
        (1, 300),  # 首次签到：100 + 200
        (2, 110),  # 100 + 10
        (5, 140),  # 100 + 40
        (7, 460),  # 100 + 60 + 300
        (15, 240),  # 100 + 140
        (30, 1750),  # 100 + 150 + 1500
        (210, 2050),  # 100 + 150 + 300 + 1500
    ],
)
def test_documented_sample_amounts(streak: int, total: int):
    first = streak == 1
    assert reward(streak, first=first).total == total


def test_random_bonus_is_bounded_and_injectable():
    assert roll_random_bonus(COINS, rng=_Rng(7)) == 7
    assert roll_random_bonus(COINS, rng=_Rng(9999)) == COINS.random_bonus_max
    assert expected_random_bonus(COINS) == 25


def test_backfill_uses_expected_random_and_no_welcome_bonus():
    first = backfill_reward_for_record(COINS, 1)
    assert first.random_bonus == 25
    assert first.first_bonus == 0
    assert first.total == 125

    assert backfill_reward_for_record(COINS, 7).total == 100 + 25 + 60 + 300
    assert backfill_reward_for_record(COINS, 30).total == 100 + 25 + 150 + 1500
    assert backfill_reward_for_record(COINS, 0).total == 125


def test_backfill_is_deterministic():
    for streak in (0, 1, 6, 7, 30, 210):
        assert backfill_reward_for_record(COINS, streak) == backfill_reward_for_record(
            COINS,
            streak,
        )


class _Rng:
    def __init__(self, value: int) -> None:
        self.value = value

    def randint(self, low: int, high: int) -> int:
        return min(max(self.value, low), high)
