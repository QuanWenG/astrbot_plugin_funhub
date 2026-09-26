"""签到事务：首签、重复、连签递推、周期奖励、等级不被改写。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funhub.checkin import auto_checkin_reply
from funhub.players import utc_now_text

from .conftest import identity

HK = timezone(timedelta(hours=8))
DAY = datetime(2026, 1, 10, 12, 0, tzinfo=HK)


def at(day_offset: int, hour: int = 12) -> datetime:
    return (DAY + timedelta(days=day_offset)).replace(hour=hour)


async def test_first_checkin(service):
    outcome = await service.check_in(identity(), now=DAY)
    assert outcome.already_checked is False
    assert outcome.streak_days == 1
    assert outcome.reward is not None
    assert outcome.reward.first_bonus == 200
    assert outcome.coins_gain == 300
    assert outcome.player.coins == 300
    assert outcome.player.total_coins == 300
    assert outcome.player.total_checkins == 1
    assert outcome.player.best_streak == 1
    assert outcome.player.last_checkin_date == "2026-01-10"
    assert outcome.player.level == 1


async def test_second_checkin_same_day_is_a_no_op(service, database):
    first = await service.check_in(identity(), now=DAY)
    writes_after_first = database.write_transactions

    second = await service.check_in(identity(), now=DAY.replace(hour=23))
    assert second.already_checked is True
    assert second.coins_gain == first.coins_gain
    assert second.streak_days == 1
    assert second.player.coins == first.player.coins
    assert second.player.total_checkins == 1
    # 当天重复签到走只读快速路径，不再抢写锁（自动签到会频繁命中这里）
    assert database.write_transactions == writes_after_first


async def test_streak_grows_on_consecutive_days(service):
    await service.check_in(identity(), now=at(0))
    second = await service.check_in(identity(), now=at(1))
    assert second.streak_days == 2
    assert second.coins_gain == 110  # 100 基础 + 10 连签
    assert second.reward is not None
    assert second.reward.first_bonus == 0
    assert second.player.best_streak == 2


async def test_streak_resets_after_a_gap(service):
    await service.check_in(identity(), now=at(0))
    third = await service.check_in(identity(), now=at(3))
    assert third.streak_days == 1
    assert third.coins_gain == 100
    assert third.player.best_streak == 1


async def test_day_reset_hour_splits_one_calendar_day(service):
    # 03:00 属于前一天，05:00 属于新的一天 → 同一天里可以签两次
    early = await service.check_in(identity(), now=at(0, hour=3))
    late = await service.check_in(identity(), now=at(0, hour=5))
    assert early.player.last_checkin_date == "2026-01-09"
    assert late.player.last_checkin_date == "2026-01-10"
    assert late.streak_days == 2
    assert late.player.total_checkins == 2


async def test_seven_day_cycle_pays_the_short_bonus(service):
    for offset in range(7):
        outcome = await service.check_in(identity(), now=at(offset))
    assert outcome.streak_days == 7
    assert [hit.days for hit in outcome.cycle_hits] == [7]
    assert outcome.coins_gain == 460  # 100 + 60 连签 + 300 周期
    assert outcome.player.coins == 300 + 110 + 120 + 130 + 140 + 150 + 460


async def test_thirty_day_cycle_pays_the_long_bonus(make_service):
    service = make_service(0)
    for offset in range(30):
        outcome = await service.check_in(identity(), now=at(offset))
    assert outcome.streak_days == 30
    assert [(hit.days, hit.bonus) for hit in outcome.cycle_hits] == [(30, 1500)]
    assert outcome.coins_gain == 1750  # 100 + 150 连签封顶 + 1500


async def test_plain_day_has_no_cycle_line(service):
    for offset in range(6):
        outcome = await service.check_in(identity(), now=at(offset))
    assert outcome.streak_days == 6
    assert outcome.cycle_hits == ()


async def test_coins_and_ledger_stay_in_sync(service, database):
    for offset in range(10):
        await service.check_in(identity(), now=at(offset))
    row = await database.fetchone(
        "SELECT total_coins FROM players WHERE user_id = '200'",
    )
    granted = await database.fetchone(
        "SELECT COALESCE(SUM(coins_gain), 0) AS granted FROM checkins",
    )
    assert int(row["total_coins"]) == int(granted["granted"])


async def test_checkin_never_touches_level(service, database):
    await database.execute(
        "INSERT INTO players (platform, group_id, user_id, nickname, level, created_at, updated_at) "
        "VALUES ('aiocqhttp', '100', '200', '龟甲', 42, ?, ?)",
        (utc_now_text(), utc_now_text()),
    )
    await database.connection.commit()
    outcome = await service.check_in(identity(), now=DAY)
    assert outcome.player.level == 42


async def test_auto_source_is_recorded(service, database):
    await service.check_in(identity(), source="auto", now=DAY)
    row = await database.fetchone("SELECT source FROM checkins")
    assert row["source"] == "auto"


async def test_unknown_source_is_rejected(service):
    with pytest.raises(ValueError):
        await service.check_in(identity(), source="telepathy", now=DAY)


async def test_players_are_separated_by_group(service):
    await service.check_in(identity(group_id="100"), now=DAY)
    other = await service.check_in(identity(group_id="200"), now=DAY)
    assert other.player.coins == 300
    assert other.streak_days == 1


async def test_nickname_follows_the_platform_name(service):
    await service.check_in(identity(nickname="龟甲"), now=DAY)
    renamed = await service.check_in(
        identity(nickname="龟甲改名了"),
        now=at(1),
    )
    assert renamed.player.nickname == "龟甲改名了"


async def test_empty_nickname_does_not_erase_stored_name(service):
    await service.check_in(identity(nickname="龟甲"), now=DAY)
    again = await service.check_in(identity(nickname=""), now=at(1))
    assert again.player.nickname == "龟甲"


async def test_random_source_is_used_exactly_once(make_service):
    service = make_service(50)
    first = await service.check_in(identity(), now=DAY)
    assert first.reward is not None
    assert first.reward.random_bonus == 50
    assert first.coins_gain == 350
    second = await service.check_in(identity(), now=at(1))
    assert second.reward is not None
    assert second.reward.random_bonus == 50
    assert second.coins_gain == 160  # 100 + 50 + 10


# ---------------------------------------------------------------------------
# 自动签到的出声策略（checkin.auto_checkin_reply）
# ---------------------------------------------------------------------------


async def test_auto_reply_always_shows_the_card(service):
    outcome = await service.check_in(identity(), source="auto", now=DAY)
    reply = auto_checkin_reply(outcome, mode="always", woken=False)
    assert reply is not None
    assert reply.splitlines()[0] == "签到成功，+300 币"


async def test_auto_reply_cycle_mode_stays_quiet_on_plain_days(service):
    outcome = await service.check_in(identity(), source="auto", now=DAY)
    assert auto_checkin_reply(outcome, mode="cycle", woken=False) is None


async def test_auto_reply_cycle_mode_speaks_on_a_cycle_day(make_service):
    service = make_service(0)
    for offset in range(7):
        outcome = await service.check_in(identity(), source="auto", now=at(offset))
    reply = auto_checkin_reply(outcome, mode="cycle", woken=False)
    assert reply is not None
    assert "连签 7 天，额外 +300 币" in reply


async def test_auto_reply_silent_mode_never_speaks(service):
    outcome = await service.check_in(identity(), source="auto", now=DAY)
    assert auto_checkin_reply(outcome, mode="silent", woken=False) is None


async def test_auto_reply_keeps_quiet_on_woken_messages(service):
    """被 @ / 带唤醒前缀的消息自带回复，自动签到不抢话（但签到照常入库）。"""
    outcome = await service.check_in(identity(), source="auto", now=DAY)
    assert auto_checkin_reply(outcome, mode="always", woken=True) is None
    assert outcome.player.total_checkins == 1


async def test_auto_reply_is_none_when_already_checked(service):
    await service.check_in(identity(), now=DAY)
    again = await service.check_in(identity(), source="auto", now=DAY)
    assert again.already_checked is True
    assert auto_checkin_reply(again, mode="always", woken=False) is None
