"""玩家与签到记录的读写。

每个玩家的身份是 ``(platform, group_id, user_id)`` —— 与老插件 ``users`` 表的唯一
约束一致，所以老数据可以 1:1 导入，白名单里的多个群之间互不影响。

本模块只做数据存取与聚合，不含数值政策（在 ``coins.py``）与用例编排（在
``checkin.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .db import Database

PLAYER_COLUMNS = """
    id, platform, group_id, user_id, nickname, level, coins, total_coins,
    total_checkins, streak_days, best_streak, last_checkin_date
"""


def utc_now_text() -> str:
    """入库时间戳。历史日期用结算日表示，这里只记录写入时刻。

    一律用 UTC ISO 文本，这样不同记录之间可以直接做字符串比较（``muted_until``
    就是靠这个判断"还在禁言中"）。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_after_text(seconds: int) -> str:
    """``seconds`` 秒之后的 UTC ISO 文本。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat(
        timespec="seconds",
    )


@dataclass(frozen=True, slots=True)
class PlayerIdentity:
    platform: str
    group_id: str
    user_id: str
    nickname: str = ""


@dataclass(frozen=True, slots=True)
class Player:
    id: int
    platform: str
    group_id: str
    user_id: str
    nickname: str
    level: int
    coins: int
    total_coins: int
    total_checkins: int
    streak_days: int
    best_streak: int
    last_checkin_date: str | None

    @property
    def display_name(self) -> str:
        return self.nickname or self.user_id or "未知用户"


def row_to_player(row: aiosqlite.Row) -> Player:
    return Player(
        id=int(row["id"]),
        platform=str(row["platform"]),
        group_id=str(row["group_id"]),
        user_id=str(row["user_id"]),
        nickname=str(row["nickname"]),
        level=int(row["level"]),
        coins=int(row["coins"]),
        total_coins=int(row["total_coins"]),
        total_checkins=int(row["total_checkins"]),
        streak_days=int(row["streak_days"]),
        best_streak=int(row["best_streak"]),
        last_checkin_date=(
            None if row["last_checkin_date"] is None else str(row["last_checkin_date"])
        ),
    )


class PlayerRepository:
    def __init__(self, database: Database | None = None) -> None:
        # 导入流程会在一个外部事务里复用本类的方法，此时不需要库句柄。
        self.database = database

    def _require_database(self) -> Database:
        if self.database is None:
            raise RuntimeError("该操作需要数据库句柄")
        return self.database

    # ---------------------------------------------------------------- 读取

    async def get(self, identity: PlayerIdentity) -> Player | None:
        row = await self._require_database().fetchone(
            f"SELECT {PLAYER_COLUMNS} FROM players "
            "WHERE platform = ? AND group_id = ? AND user_id = ?",
            (identity.platform, identity.group_id, identity.user_id),
        )
        return None if row is None else row_to_player(row)

    async def get_in_db(
        self,
        connection: aiosqlite.Connection,
        identity: PlayerIdentity,
    ) -> Player | None:
        cursor = await connection.execute(
            f"SELECT {PLAYER_COLUMNS} FROM players "
            "WHERE platform = ? AND group_id = ? AND user_id = ?",
            (identity.platform, identity.group_id, identity.user_id),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return None if row is None else row_to_player(row)

    async def find_checkin(
        self,
        *,
        player_pk: int,
        checkin_date: str,
    ) -> aiosqlite.Row | None:
        """按天读一条签到记录（只读，不抢写锁）。"""
        return await self._require_database().fetchone(
            "SELECT streak_days, coins_gain FROM checkins "
            "WHERE player_pk = ? AND checkin_date = ?",
            (player_pk, checkin_date),
        )

    # ---------------------------------------------------------------- 写入

    async def get_or_create_in_db(
        self,
        connection: aiosqlite.Connection,
        identity: PlayerIdentity,
        *,
        nickname: str | None = None,
    ) -> tuple[Player, bool]:
        """返回 ``(玩家, 是否本次新建)``。

        昵称会跟随平台上的展示名刷新（本插件没有"登记昵称"指令），但空昵称不会
        覆盖已有值，免得把导入进来的名字抹掉。
        """
        player = await self.get_in_db(connection, identity)
        now = utc_now_text()
        effective_nickname = (nickname if nickname is not None else identity.nickname) or ""
        effective_nickname = " ".join(effective_nickname.split())

        if player is not None:
            if effective_nickname and effective_nickname != player.nickname:
                await connection.execute(
                    "UPDATE players SET nickname = ?, updated_at = ? WHERE id = ?",
                    (effective_nickname, now, player.id),
                )
                player = await self.reload_in_db(connection, player.id)
            return player, False

        await connection.execute(
            """
            INSERT INTO players (
                platform, group_id, user_id, nickname, level, coins, total_coins,
                total_checkins, streak_days, best_streak, last_checkin_date,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 0, 0, 0, 0, 0, NULL, ?, ?)
            """,
            (
                identity.platform,
                identity.group_id,
                identity.user_id,
                effective_nickname or identity.user_id,
                now,
                now,
            ),
        )
        created = await self.get_in_db(connection, identity)
        if created is None:  # pragma: no cover - 插入后必然可读，纯防御
            raise RuntimeError("玩家写入失败")
        return created, True

    async def reload_in_db(
        self,
        connection: aiosqlite.Connection,
        player_pk: int,
    ) -> Player:
        cursor = await connection.execute(
            f"SELECT {PLAYER_COLUMNS} FROM players WHERE id = ?",
            (player_pk,),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        if row is None:  # pragma: no cover - 防御
            raise RuntimeError(f"玩家 {player_pk} 不存在")
        return row_to_player(row)

    async def update_after_checkin_in_db(
        self,
        connection: aiosqlite.Connection,
        *,
        player_pk: int,
        streak_days: int,
        checkin_date: str,
    ) -> None:
        """在签到事务里更新连签与累计字段（币的变动走 :meth:`apply_coin_delta_in_db`）。

        ``total_checkins`` 直接按 checkins 表重算，避免手工累加漂移。等级永远不在
        这里出现：它只由导入写入。
        """
        now = utc_now_text()
        await connection.execute(
            """
            UPDATE players SET
                total_checkins = (
                    SELECT COUNT(*) FROM checkins WHERE player_pk = players.id
                ),
                streak_days = ?,
                best_streak = MAX(best_streak, ?),
                last_checkin_date = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (streak_days, streak_days, checkin_date, now, player_pk),
        )

    async def apply_coin_delta_in_db(
        self,
        connection: aiosqlite.Connection,
        *,
        player_pk: int,
        delta: int,
        reason: str,
        ref: str = "",
    ) -> int:
        """改余额并记一条流水，返回变动后的余额。

        ``total_coins`` 是"累计获得"，只随正数增长（决斗输掉的币不会让它变小）。
        余额不足时按实际余额扣，不会扣成负数。
        """
        if delta == 0:
            return await self._coins_in_db(connection, player_pk)

        available = await self._coins_in_db(connection, player_pk)
        applied = delta if delta < 0 else int(delta)
        if applied < 0:
            applied = -min(available, -applied)
        if applied == 0:
            return available

        now = utc_now_text()
        await connection.execute(
            """
            UPDATE players SET
                coins = coins + ?,
                total_coins = total_coins + MAX(?, 0),
                updated_at = ?
            WHERE id = ?
            """,
            (applied, applied, now, player_pk),
        )
        balance = await self._coins_in_db(connection, player_pk)
        await connection.execute(
            """
            INSERT INTO coin_ledger (player_pk, delta, balance_after, reason, ref, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (player_pk, applied, balance, reason, ref, now),
        )
        return balance

    async def _coins_in_db(self, connection: aiosqlite.Connection, player_pk: int) -> int:
        cursor = await connection.execute(
            "SELECT coins FROM players WHERE id = ?",
            (player_pk,),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return int(row["coins"]) if row else 0

    # ---------------------------------------------------------------- 禁言记录

    async def record_mute_in_db(
        self,
        connection: aiosqlite.Connection,
        *,
        player_pk: int,
        expires_at: str,
    ) -> None:
        """记下我们自己发出的禁言，用于判断"这个人已经在禁言中"。"""
        now = utc_now_text()
        await connection.execute(
            """
            INSERT INTO duel_mutes (player_pk, expires_at, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(player_pk) DO UPDATE SET
                expires_at = excluded.expires_at,
                created_at = excluded.created_at
            """,
            (player_pk, expires_at, now),
        )

    async def muted_until(self, player_pk: int) -> str | None:
        """还没到期的禁言结束时间；没有就返回 None。"""
        row = await self._require_database().fetchone(
            "SELECT expires_at FROM duel_mutes WHERE player_pk = ?",
            (player_pk,),
        )
        if row is None:
            return None
        expires_at = str(row["expires_at"])
        return expires_at if expires_at > utc_now_text() else None

    async def refresh_aggregates_in_db(
        self,
        connection: aiosqlite.Connection,
        *,
        player_pk: int,
    ) -> None:
        """按 checkins 表重算累计字段，并按最新记录推进连签。

        导入用：补发的币走 :meth:`apply_coin_delta_in_db` 记流水，
        这里只负责"最新一条签到记录"决定连签能否续上。
        """
        cursor = await connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(MAX(streak_days), 0) AS best
            FROM checkins WHERE player_pk = ?
            """,
            (player_pk,),
        )
        try:
            aggregate = await cursor.fetchone()
        finally:
            await cursor.close()

        cursor = await connection.execute(
            """
            SELECT checkin_date, streak_days FROM checkins
            WHERE player_pk = ? AND checkin_date IS NOT NULL
            ORDER BY checkin_date DESC LIMIT 1
            """,
            (player_pk,),
        )
        try:
            latest = await cursor.fetchone()
        finally:
            await cursor.close()

        now = utc_now_text()
        await connection.execute(
            """
            UPDATE players SET
                total_checkins = ?,
                best_streak = MAX(best_streak, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (
                int(aggregate["total"]) if aggregate else 0,
                int(aggregate["best"]) if aggregate else 0,
                now,
                player_pk,
            ),
        )
        if latest is not None:
            latest_date = str(latest["checkin_date"])
            await connection.execute(
                """
                UPDATE players SET streak_days = ?, last_checkin_date = ?
                WHERE id = ?
                  AND (last_checkin_date IS NULL OR last_checkin_date < ?)
                """,
                (int(latest["streak_days"]), latest_date, player_pk, latest_date),
            )

    async def set_level_in_db(
        self,
        connection: aiosqlite.Connection,
        *,
        player_pk: int,
        level: int,
    ) -> bool:
        """导入时抬高等级（只升不降），返回是否发生变化。"""
        cursor = await connection.execute(
            "SELECT level FROM players WHERE id = ?",
            (player_pk,),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        current = int(row["level"]) if row else 1
        if level <= current:
            return False
        await connection.execute(
            "UPDATE players SET level = ?, updated_at = ? WHERE id = ?",
            (level, utc_now_text(), player_pk),
        )
        return True

    # ---------------------------------------------------------------- 排行

    def _order_clause(self, by_coins: bool) -> str:
        """排行排序键。

        ``by_coins`` 按**存款**（``coins``，当前余额）排，不是累计获得（``total_coins``）
        —— 输掉的钱会真的把名次拉下来。
        """
        if by_coins:
            return "coins DESC, level DESC, total_checkins DESC, id ASC"
        return "level DESC, coins DESC, total_checkins DESC, id ASC"

    async def ranking(
        self,
        *,
        platform: str,
        group_id: str,
        limit: int,
        by_coins: bool = False,
    ) -> list[Player]:
        rows = await self._require_database().fetchall(
            f"SELECT {PLAYER_COLUMNS} FROM players "
            "WHERE platform = ? AND group_id = ? AND total_checkins > 0 "
            f"ORDER BY {self._order_clause(by_coins)} LIMIT ?",
            (platform, group_id, max(int(limit), 1)),
        )
        return [row_to_player(row) for row in rows]

    async def rank_of(self, player: Player, *, by_coins: bool = True) -> int:
        """名次，排序键与 :meth:`ranking` 一致（并列按 id 定先后）。

        默认按存款（``coins``）排 —— 面板展示的"本群第 N"就是这个。
        """
        first = "coins" if by_coins else "level"
        second = "level" if by_coins else "coins"
        first_value = player.coins if by_coins else player.level
        second_value = player.level if by_coins else player.coins
        row = await self._require_database().fetchone(
            f"""
            SELECT COUNT(*) AS ahead FROM players
            WHERE platform = ? AND group_id = ? AND total_checkins > 0
              AND (
                    {first} > ?
                 OR ({first} = ? AND {second} > ?)
                 OR ({first} = ? AND {second} = ? AND total_checkins > ?)
                 OR ({first} = ? AND {second} = ? AND total_checkins = ? AND id < ?)
              )
            """,
            (
                player.platform,
                player.group_id,
                first_value,
                first_value,
                second_value,
                first_value,
                second_value,
                player.total_checkins,
                first_value,
                second_value,
                player.total_checkins,
                player.id,
            ),
        )
        ahead = int(row["ahead"]) if row else 0
        return ahead + 1

    async def count_in_group(self, *, platform: str, group_id: str) -> int:
        row = await self._require_database().fetchone(
            "SELECT COUNT(*) AS total FROM players "
            "WHERE platform = ? AND group_id = ? AND total_checkins > 0",
            (platform, group_id),
        )
        return int(row["total"]) if row else 0

    # ---------------------------------------------------------------- 校验

    async def coins_invariant_in_db(self, player_pk: int) -> tuple[int, int, int]:
        """返回 ``(余额, 累计获得, 流水汇总)``，供账目自检。

        正常时 ``余额 == 流水汇总`` 且 ``累计获得 == 正流水之和``。
        """
        player = await self._require_database().fetchone(
            "SELECT coins, total_coins FROM players WHERE id = ?",
            (player_pk,),
        )
        ledger = await self._require_database().fetchone(
            """
            SELECT COALESCE(SUM(delta), 0) AS balance,
                   COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0) AS earned
            FROM coin_ledger WHERE player_pk = ?
            """,
            (player_pk,),
        )
        return (
            int(player["coins"]) if player else 0,
            int(player["total_coins"]) if player else 0,
            int(ledger["balance"]) if ledger else 0,
        )


def identity_from_values(
    *,
    platform: Any,
    group_id: Any,
    user_id: Any,
    nickname: Any = "",
) -> PlayerIdentity:
    """把外部数据（事件、导入 JSON）收敛成身份三元组。"""
    return PlayerIdentity(
        platform=str(platform or "").strip(),
        group_id=str(group_id or "").strip(),
        user_id=str(user_id or "").strip(),
        nickname=" ".join(str(nickname or "").split()),
    )
