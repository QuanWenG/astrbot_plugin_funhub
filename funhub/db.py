"""SQLite 访问层。

一个插件只开一条 aiosqlite 连接：写操作由 ``transaction()`` 里的 asyncio 锁串行化
（``BEGIN IMMEDIATE`` 抢写锁，避免两个并发的"当天首次签到"都拿到写权限），
读操作直接复用同一条连接。

``checkins.coins_gain`` 是龟龟币的唯一凭据，因此始终满足不变量::

    players.total_coins == SUM(checkins.coins_gain)

导入、签到、测试都依赖这条不变量，所以任何改动金额的写入都必须同时更新两边。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 2
"""当前表结构版本，写入 meta.schema_version。

v2 起龟龟币会在玩家之间流动（决斗），因此新增 ``coin_ledger`` 作为唯一凭据：
``players.coins == SUM(delta)``、``players.total_coins == SUM(正 delta)``。
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 1,
    coins INTEGER NOT NULL DEFAULT 0,
    total_coins INTEGER NOT NULL DEFAULT 0,
    total_checkins INTEGER NOT NULL DEFAULT 0,
    streak_days INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    last_checkin_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, group_id, user_id)
);

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_pk INTEGER NOT NULL,
    checkin_date TEXT NOT NULL,
    streak_days INTEGER NOT NULL,
    coins_gain INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'command',
    created_at TEXT NOT NULL,
    UNIQUE(player_pk, checkin_date),
    FOREIGN KEY(player_pk) REFERENCES players(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 龟龟币流水。签到、导入补发、决斗输赢都写这里，是余额的唯一凭据。
CREATE TABLE IF NOT EXISTS coin_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_pk INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(player_pk) REFERENCES players(id) ON DELETE CASCADE
);

-- 我们自己发出的禁言记录（决斗用）。平台查不到成员禁言状态时靠它判断"已经被禁言"。
CREATE TABLE IF NOT EXISTS duel_mutes (
    player_pk INTEGER PRIMARY KEY,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(player_pk) REFERENCES players(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checkins_player_date
    ON checkins(player_pk, checkin_date);

CREATE INDEX IF NOT EXISTS idx_players_group_rank
    ON players(group_id, level DESC, total_coins DESC);

CREATE INDEX IF NOT EXISTS idx_coin_ledger_player
    ON coin_ledger(player_pk, id);
"""


class Database:
    """懒连接的 SQLite 句柄。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        #: 已开启的写事务数量。自动签到会在每条群消息上跑一次，靠它确认
        #: "今天已经签过"的常见路径不会去抢写锁（测试也用它）。
        self.write_transactions = 0

    # ---------------------------------------------------------------- 连接

    @property
    def connected(self) -> bool:
        return self._connection is not None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("数据库尚未连接，请先 await Database.connect()")
        return self._connection

    async def connect(self) -> aiosqlite.Connection:
        """建库、建表、打开连接。重复调用是幂等的。"""
        if self._connection is not None:
            return self._connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.executescript(SCHEMA_SQL)
            await connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        return connection

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    # ---------------------------------------------------------------- 事务

    @asynccontextmanager
    async def transaction(self, *, rollback: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        """串行化的写事务。

        Args:
            rollback: 为真时在退出时回滚（导入预览用），其余情况提交。
        """
        await self.connect()
        async with self._write_lock:
            connection = self.connection
            self.write_transactions += 1
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            if rollback:
                await connection.rollback()
            else:
                await connection.commit()

    # ---------------------------------------------------------------- 查询

    async def execute(self, sql: str, params: Sequence[object] = ()) -> aiosqlite.Cursor:
        await self.connect()
        return await self.connection.execute(sql, tuple(params))

    async def fetchone(self, sql: str, params: Sequence[object] = ()) -> aiosqlite.Row | None:
        cursor = await self.execute(sql, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def fetchall(self, sql: str, params: Sequence[object] = ()) -> list[aiosqlite.Row]:
        cursor = await self.execute(sql, params)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    # ---------------------------------------------------------------- meta

    async def get_meta(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    async def set_meta(self, key: str, value: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
