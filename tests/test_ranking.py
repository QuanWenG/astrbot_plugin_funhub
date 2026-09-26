"""面板与排行：排序键、名次、截断、空数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from funhub.checkin import days_to_short_cycle

from .conftest import identity

HK = timezone(timedelta(hours=8))
DAY = datetime(2026, 1, 10, 12, 0, tzinfo=HK)


async def seed(
    service,
    database,
    *,
    group: str,
    user: str,
    level: int = 1,
    coins: int | None = None,
) -> None:
    """造一个"签到过一天"的玩家，并按需调整等级 / 累计币。"""
    await service.check_in(identity(group_id=group, user_id=user, nickname=user), now=DAY)
    if level != 1:
        await database.execute(
            "UPDATE players SET level = ? WHERE group_id = ? AND user_id = ?",
            (level, group, user),
        )
    if coins is not None:
        await database.execute(
            "UPDATE players SET total_coins = ?, coins = ? "
            "WHERE group_id = ? AND user_id = ?",
            (coins, coins, group, user),
        )
    await database.connection.commit()


async def test_ranking_sorts_by_level_then_coins(service, database):
    await seed(service, database, group="100", user="a", level=5, coins=300)
    await seed(service, database, group="100", user="b", level=9, coins=100)
    await seed(service, database, group="100", user="c", level=9, coins=200)
    entries = await service.ranking(identity(group_id="100"))
    assert [player.user_id for _, player in entries] == ["c", "b", "a"]
    assert [rank for rank, _ in entries] == [1, 2, 3]


async def test_ranking_by_coins(service, database):
    await seed(service, database, group="100", user="a", level=5, coins=900)
    await seed(service, database, group="100", user="b", level=9, coins=100)
    entries = await service.ranking(identity(group_id="100"), by_coins=True)
    assert [player.user_id for _, player in entries] == ["a", "b"]


async def test_coin_ranking_uses_the_balance_not_lifetime_earnings(service, database):
    """龟龟币排行看存款：赢来的算、输掉的会掉名次。"""
    # a 累计赚得多，但输掉之后只剩 100
    await seed(service, database, group="100", user="a", level=9, coins=5000)
    await seed(service, database, group="100", user="b", level=1, coins=1000)
    await database.execute(
        "UPDATE players SET coins = 100 WHERE user_id = 'a'",
    )
    await database.connection.commit()

    entries = await service.ranking(identity(group_id="100"), by_coins=True)
    assert [player.user_id for _, player in entries] == ["b", "a"]
    assert [player.coins for _, player in entries] == [1000, 100]

    # 面板/名次查询也按存款算
    a = await service.players.get(identity(group_id="100", user_id="a"))
    assert a is not None
    assert await service.players.rank_of(a, by_coins=True) == 2
    assert await service.players.rank_of(a, by_coins=False) == 1  # 等级榜仍是第一


async def test_level_ranking_breaks_ties_by_balance(service, database):
    await seed(service, database, group="100", user="a", level=9, coins=100)
    await seed(service, database, group="100", user="b", level=9, coins=800)
    entries = await service.ranking(identity(group_id="100"), by_coins=False)
    assert [player.user_id for _, player in entries] == ["b", "a"]


async def test_ranking_is_scoped_to_the_group(service, database):
    await seed(service, database, group="100", user="a", level=5)
    await seed(service, database, group="200", user="b", level=9)
    entries = await service.ranking(identity(group_id="100"))
    assert [player.user_id for _, player in entries] == ["a"]


async def test_players_without_checkins_are_hidden(service, database):
    await seed(service, database, group="100", user="a", level=5)
    await database.execute(
        "INSERT INTO players (platform, group_id, user_id, nickname, level, created_at, updated_at) "
        "VALUES ('aiocqhttp', '100', 'ghost', '幽灵', 99, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
    )
    await database.connection.commit()
    entries = await service.ranking(identity(group_id="100"))
    assert [player.user_id for _, player in entries] == ["a"]


async def test_ranking_respects_ranking_size(service, database):
    for index in range(5):
        await seed(service, database, group="100", user=f"u{index}", level=index + 1)
    entries = await service.ranking(identity(group_id="100"), limit=2)
    assert len(entries) == 2


async def test_profile_reports_rank_and_cycle_distance(service, database):
    await seed(service, database, group="100", user="a", level=9)
    await seed(service, database, group="100", user="b", level=5)
    view = await service.profile(identity(group_id="100", user_id="b"))
    assert view.rank == 2
    assert view.group_total == 2
    assert view.cycle_days == 7
    assert view.days_to_cycle == 6  # 连签 1 天，距 7 天周期还差 6 天


async def test_profile_rank_follows_the_coin_ranking(service, database):
    """面板里的"本群第 N"是龟龟币（存款）名次，不是等级名次。"""
    await seed(service, database, group="100", user="高等级穷鬼", level=30, coins=50)
    await seed(service, database, group="100", user="低等级富豪", level=1, coins=9000)

    poor = await service.profile(identity(group_id="100", user_id="高等级穷鬼"))
    rich = await service.profile(identity(group_id="100", user_id="低等级富豪"))

    assert rich.rank == 1
    assert poor.rank == 2


async def test_profile_creates_a_missing_player(service):
    view = await service.profile(identity(user_id="newcomer", nickname="新人"))
    assert view.player.total_checkins == 0
    assert view.rank == 1
    assert view.days_to_cycle == 7


async def test_rank_lookup_for_another_member(service, database):
    await seed(service, database, group="100", user="a", level=9)
    await seed(service, database, group="100", user="b", level=5)
    target = await service.players.get(identity(group_id="100", user_id="a"))
    assert target is not None
    assert await service.rank_of(target) == 1


def test_days_to_short_cycle_helper():
    assert days_to_short_cycle(streak_days=0, short_cycle_days=7) == 7
    assert days_to_short_cycle(streak_days=1, short_cycle_days=7) == 6
    assert days_to_short_cycle(streak_days=6, short_cycle_days=7) == 1
    assert days_to_short_cycle(streak_days=7, short_cycle_days=7) == 7
    assert days_to_short_cycle(streak_days=9, short_cycle_days=7) == 5


async def test_render_ranking_and_rank_cards(service, database):
    await seed(service, database, group="100", user="a", level=9)
    entries = await service.ranking(identity(group_id="100"))
    text = service.render_ranking(entries, by_coins=False)
    assert text.splitlines()[0] == "本群排行"
    assert "a Lv.9" in text
    assert service.render_ranking(entries, by_coins=True).splitlines()[0] == "本群排行（按龟龟币）"

    assert service.render_ranking([], by_coins=True) == "本群还没有人签到过"

    player = await service.players.get(identity(group_id="100", user_id="a"))
    assert player is not None
    assert service.render_rank_of(player, 1).splitlines()[0].endswith("本群第 1")
