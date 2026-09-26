"""签到 / 面板 / 排行的用例编排。

服务层持有"政策"：连签怎么递推、什么时候发周期奖、面板要展示哪些字段。
数值公式在 ``coins.py``，落库细节在 ``players.py``。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, tzinfo

import aiosqlite

from . import messages
from .clock import previous_day, resolve_zone, settlement_day_key
from .coins import CheckinReward, CycleHit, compute_reward, roll_random_bonus
from .config import Settings
from .db import Database
from .players import Player, PlayerIdentity, PlayerRepository, utc_now_text

CHECKIN_SOURCES = ("command", "auto", "import")


@dataclass(frozen=True, slots=True)
class CheckinOutcome:
    """一次签到的结果。``reward`` 为 None 表示今天已经签过。

    ``source`` 为 ``"cached"`` 时表示这次是只读回放（快速路径），没有真的执行签到。
    """

    player: Player
    already_checked: bool
    streak_days: int
    coins_gain: int
    reward: CheckinReward | None
    source: str

    @property
    def cycle_hits(self) -> tuple[CycleHit, ...]:
        return () if self.reward is None else self.reward.cycle_hits


@dataclass(frozen=True, slots=True)
class ProfileView:
    player: Player
    rank: int
    group_total: int
    cycle_days: int
    days_to_cycle: int


def days_to_short_cycle(*, streak_days: int, short_cycle_days: int) -> int:
    """距离下一个短周期还有几天。刚到周期当天时返回完整周期长度。"""
    if short_cycle_days <= 0:
        return 0
    streak = max(int(streak_days), 0)
    remainder = streak % short_cycle_days
    return short_cycle_days if remainder == 0 else short_cycle_days - remainder


def auto_checkin_reply(
    outcome: CheckinOutcome,
    *,
    mode: str,
    woken: bool,
) -> str | None:
    """自动签到要不要出声、出声说什么。

    Args:
        outcome: 自动签到的结果。
        mode: ``always`` 每次回签到卡 / ``cycle`` 只在命中周期时出声 / ``silent`` 完全静默。
        woken: 这条消息是否被唤醒（``@机器人``、唤醒前缀、引用机器人）。
            被唤醒的消息通常自带回复（本插件指令、别的插件指令、或者 LLM），
            自动签到就不抢话了 —— 但签到本身照常入库。
    """
    if outcome.already_checked or outcome.reward is None:
        return None
    if mode == "silent" or woken:
        return None
    if mode == "cycle":
        if not outcome.reward.cycle_hits:
            return None
        return messages.auto_checkin_cycles(
            streak_days=outcome.streak_days,
            cycle_hits=outcome.reward.cycle_hits,
        )
    return messages.checkin_card(
        total=outcome.coins_gain,
        streak_days=outcome.streak_days,
        coins_total=outcome.player.coins,
        cycle_hits=outcome.reward.cycle_hits,
    )


class CheckinService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.rng = rng
        self.players = PlayerRepository(database)
        self.zone: tzinfo
        self.zone, self.zone_warning = resolve_zone(settings.checkin.timezone)

    # ---------------------------------------------------------------- 签到

    async def check_in(
        self,
        identity: PlayerIdentity,
        *,
        source: str = "command",
        now: datetime | None = None,
    ) -> CheckinOutcome:
        """结算一次签到。

        整个过程在一个 ``BEGIN IMMEDIATE`` 事务里完成：唯一约束保证同一天不会发
        两次币，写锁保证两个并发的"当天首条消息"不会各发一次。
        """
        if source not in CHECKIN_SOURCES:
            raise ValueError(f"未知签到来源：{source}")
        day_key = settlement_day_key(
            now,
            zone=self.zone,
            reset_hour=self.settings.checkin.day_reset_hour,
        )

        # 快速路径：当天已经签过就只读地回放那一条记录。自动签到会在每条群消息上
        # 调到这里，绝大多数调用都是这种情况，不该去抢 BEGIN IMMEDIATE 写锁。
        cached = await self._completed_checkin(identity, day_key)
        if cached is not None:
            return cached

        async with self.database.transaction() as connection:
            player, _ = await self.players.get_or_create_in_db(connection, identity)
            existing = await self._existing_checkin_in_db(connection, player.id, day_key)
            if existing is not None:
                return CheckinOutcome(
                    player=player,
                    already_checked=True,
                    streak_days=int(existing["streak_days"]),
                    coins_gain=int(existing["coins_gain"]),
                    reward=None,
                    source=source,
                )

            streak_days = self._next_streak(player, day_key)
            reward = compute_reward(
                self.settings.coins,
                streak_days=streak_days,
                is_first_checkin=player.total_checkins == 0,
                random_bonus=roll_random_bonus(self.settings.coins, self.rng),
            )
            await connection.execute(
                """
                INSERT INTO checkins (
                    player_pk, checkin_date, streak_days, coins_gain, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    player.id,
                    day_key,
                    streak_days,
                    reward.total,
                    source,
                    utc_now_text(),
                ),
            )
            await self.players.apply_coin_delta_in_db(
                connection,
                player_pk=player.id,
                delta=reward.total,
                reason="checkin",
                ref=day_key,
            )
            await self.players.update_after_checkin_in_db(
                connection,
                player_pk=player.id,
                streak_days=streak_days,
                checkin_date=day_key,
            )
            updated = await self.players.reload_in_db(connection, player.id)
        return CheckinOutcome(
            player=updated,
            already_checked=False,
            streak_days=streak_days,
            coins_gain=reward.total,
            reward=reward,
            source=source,
        )

    async def _completed_checkin(
        self,
        identity: PlayerIdentity,
        day_key: str,
    ) -> CheckinOutcome | None:
        """只读地判断"今天是否已经签过"，命中则直接回放当日数值。

        必须同时满足「玩家记录的最近签到日就是今天」和「今天确实有一条签到记录」，
        否则会退回到写事务里走正常流程（那里有唯一约束兜底）。
        """
        player = await self.players.get(identity)
        if player is None or player.last_checkin_date != day_key:
            return None
        row = await self.players.find_checkin(player_pk=player.id, checkin_date=day_key)
        if row is None:
            return None
        return CheckinOutcome(
            player=player,
            already_checked=True,
            streak_days=int(row["streak_days"]),
            coins_gain=int(row["coins_gain"]),
            reward=None,
            source="cached",
        )

    def _next_streak(self, player: Player, day_key: str) -> int:
        """连签递推：昨天签过就 +1，否则从 1 重新开始。"""
        if not player.last_checkin_date:
            return 1
        yesterday = previous_day(date.fromisoformat(day_key)).isoformat()
        if player.last_checkin_date == yesterday:
            return max(player.streak_days, 0) + 1
        return 1

    async def _existing_checkin_in_db(
        self,
        connection: aiosqlite.Connection,
        player_pk: int,
        day_key: str,
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(
            "SELECT streak_days, coins_gain FROM checkins "
            "WHERE player_pk = ? AND checkin_date = ?",
            (player_pk, day_key),
        )
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    # ---------------------------------------------------------------- 面板

    async def profile(
        self,
        identity: PlayerIdentity,
        *,
        now: datetime | None = None,
    ) -> ProfileView:
        del now  # 面板不依赖结算时刻，保留参数是为了与 check_in 的调用形式一致
        async with self.database.transaction() as connection:
            player, _ = await self.players.get_or_create_in_db(connection, identity)
        # 面板里的"本群第 N"用龟龟币（存款）排行
        rank = await self.players.rank_of(player, by_coins=True)
        group_total = await self.players.count_in_group(
            platform=player.platform,
            group_id=player.group_id,
        )
        short_cycle = self.settings.coins.cycle_short_days
        return ProfileView(
            player=player,
            rank=rank,
            group_total=group_total,
            cycle_days=short_cycle,
            days_to_cycle=days_to_short_cycle(
                streak_days=player.streak_days,
                short_cycle_days=short_cycle,
            ),
        )

    # ---------------------------------------------------------------- 排行

    async def ranking(
        self,
        identity: PlayerIdentity,
        *,
        by_coins: bool = False,
        limit: int | None = None,
    ) -> list[tuple[int, Player]]:
        size = self.settings.ui.ranking_size if limit is None else int(limit)
        players = await self.players.ranking(
            platform=identity.platform,
            group_id=identity.group_id,
            limit=max(size, 1),
            by_coins=by_coins,
        )
        return [(index, player) for index, player in enumerate(players, start=1)]

    async def rank_of(self, player: Player, *, by_coins: bool = True) -> int:
        return await self.players.rank_of(player, by_coins=by_coins)

    # ---------------------------------------------------------------- 渲染

    def render_checkin(self, outcome: CheckinOutcome) -> str:
        if outcome.already_checked:
            return messages.checkin_already_card(
                coins_gain=outcome.coins_gain,
                streak_days=outcome.streak_days,
                coins_total=outcome.player.coins,
            )
        reward = outcome.reward
        return messages.checkin_card(
            total=outcome.coins_gain,
            streak_days=outcome.streak_days,
            coins_total=outcome.player.coins,
            cycle_hits=() if reward is None else reward.cycle_hits,
        )

    def render_profile(self, view: ProfileView) -> str:
        return messages.profile_card(
            level=view.player.level,
            streak_days=view.player.streak_days,
            total_checkins=view.player.total_checkins,
            coins=view.player.coins,
            rank=view.rank,
            best_streak=view.player.best_streak,
            cycle_days=view.cycle_days,
            days_to_cycle=view.days_to_cycle,
        )

    def render_ranking(self, entries: list[tuple[int, Player]], *, by_coins: bool) -> str:
        return messages.ranking_card(
            [
                (rank, player.display_name, player.level, player.coins)
                for rank, player in entries
            ],
            by_coins=by_coins,
        )

    def render_rank_of(self, player: Player, rank: int) -> str:
        return messages.rank_card(
            name=player.display_name,
            rank=rank,
            level=player.level,
            coins=player.coins,
        )
