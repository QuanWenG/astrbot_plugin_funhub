"""老插件（astrbot_plugin_LevelUpPvp）签到数据的导入与龟龟币补发。

输入是老插件 ``services/export_service.py`` 导出的 JSON::

    {
      "schema_version": 1,
      "source": "checkin-records",
      "exported_at": "...",
      "checkin_day_reset_hour": 4,
      "user_count": 2,
      "checkin_count": 3,
      "users": [
        {
          "user_pk": 1, "platform": "aiocqhttp", "group_id": "123",
          "user_id": "456", "nickname": "龟龟", "level": 12,
          "exp": 34, "total_exp": 5678, "checkin_total": 3,
          "checkins": [
            {"date": "2026-01-01", "streak_days": 1, "exp_gain": 20,
             "created_at": "2026-01-01T12:00:00"}
          ],
          "created_at": "...", "updated_at": "..."
        }
      ]
    }

导入策略：

* 等级 1:1 保留（只升不降，按 ``levels.max_level`` 裁剪），签到记录逐条入库；
* 经验一律丢弃，不折算成币；
* **按历史记录补发龟龟币**：每条记录的所得用 ``coins.backfill_reward_for_record``
  计算 —— 与实时签到共用公式，唯一区别是随机奖取期望值，保证补发可复现；
* 只有**本次新插入**的记录才计账，所以重复导入同一个文件不会二次发币。

import 是关键字，所以配置字段叫 ``settings.import_``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import aiosqlite

from .coins import backfill_reward_for_record
from .config import Settings
from .db import Database
from .players import PlayerRepository, identity_from_values

LEGACY_SCHEMA_VERSION = 1
LEGACY_SOURCE = "checkin-records"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ERRORS = 50
"""报告里最多保留多少条错误明细，避免坏文件把响应体撑爆。"""


class LegacyImportError(ValueError):
    """导入输入不可用（不是合法 JSON、结构不对等）。"""


@dataclass
class ImportReport:
    files: list[str] = field(default_factory=list)
    players_new: int = 0
    players_updated: int = 0
    checkins_inserted: int = 0
    checkins_skipped: int = 0
    coins_granted: int = 0
    coins_players: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def add_error(self, message: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    def add_warning(self, message: str) -> None:
        if len(self.warnings) < MAX_ERRORS:
            self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "players_new": self.players_new,
            "players_updated": self.players_updated,
            "checkins_inserted": self.checkins_inserted,
            "checkins_skipped": self.checkins_skipped,
            "coins_granted": self.coins_granted,
            "coins_players": self.coins_players,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


def parse_export(payload: Any, *, report: ImportReport) -> list[dict[str, Any]]:
    """校验导出结构并返回 ``users`` 列表。"""
    if not isinstance(payload, dict):
        raise LegacyImportError("导出文件必须是一个 JSON 对象")
    users = payload.get("users")
    if not isinstance(users, list):
        raise LegacyImportError("导出文件缺少 users 列表")

    version = payload.get("schema_version")
    if version is not None and version != LEGACY_SCHEMA_VERSION:
        report.add_warning(
            f"schema_version={version!r} 与预期的 {LEGACY_SCHEMA_VERSION} 不同，仍按当前结构解析",
        )
    source = payload.get("source")
    if isinstance(source, str) and source and source != LEGACY_SOURCE:
        report.add_warning(f"source={source!r} 不是 {LEGACY_SOURCE!r}，仍尝试解析")
    return [user for user in users if isinstance(user, dict)]


def load_export_file(path: str | Path) -> Any:
    """读取并解析导出 JSON，失败时抛 :class:`LegacyImportError`。"""
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LegacyImportError(f"读取文件失败：{exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise LegacyImportError(f"不是合法的 JSON：{exc}") from exc


async def import_export(
    database: Database,
    payload: Any,
    settings: Settings,
    *,
    filename: str | None = None,
    dry_run: bool = False,
    backfill_coins: bool | None = None,
) -> ImportReport:
    """把一份导出数据写进新库。

    Args:
        database: 目标数据库。
        payload: 已解析的导出 JSON。
        settings: 配置（等级上限、补发开关、缺省平台）。
        filename: 报告里显示的文件名。
        dry_run: 为真时在结束时回滚，只返回"将会发生什么"。
        backfill_coins: 覆盖配置里的补发开关。
    """
    report = ImportReport(dry_run=dry_run)
    if filename:
        report.files.append(filename)
    users = parse_export(payload, report=report)
    do_backfill = settings.import_.backfill_coins if backfill_coins is None else backfill_coins

    async with database.transaction(rollback=dry_run) as connection:
        for index, user in enumerate(users):
            await _import_user(
                connection,
                user,
                index=index,
                settings=settings,
                report=report,
                backfill_coins=do_backfill,
            )
    return report


async def _import_user(
    connection: aiosqlite.Connection,
    user: dict[str, Any],
    *,
    index: int,
    settings: Settings,
    report: ImportReport,
    backfill_coins: bool,
) -> None:
    platform = _text(user.get("platform")) or settings.import_.default_platform
    group_id = _text(user.get("group_id"))
    user_id = _text(user.get("user_id"))
    if not user_id:
        report.add_error(f"users[{index}]：缺少 user_id，已跳过")
        return

    identity = identity_from_values(
        platform=platform,
        group_id=group_id,
        user_id=user_id,
        nickname=_text(user.get("nickname")),
    )
    repository = PlayerRepository()

    level = _positive_int(user.get("level"), default=1)
    level = max(1, min(level, settings.levels.max_level))
    coins_granted = 0

    player, created = await repository.get_or_create_in_db(
        connection,
        identity,
        nickname=identity.nickname,
    )
    if created:
        report.players_new += 1

    updated = await repository.set_level_in_db(
        connection,
        player_pk=player.id,
        level=level,
    )

    records = user.get("checkins")
    if not isinstance(records, list):
        if records is not None:
            report.add_error(f"users[{index}]：checkins 不是列表，已忽略该字段")
        records = []

    for record_index, record in enumerate(records):
        if not isinstance(record, dict):
            report.add_error(f"users[{index}].checkins[{record_index}]：不是对象，已跳过")
            continue
        raw_date = _text(record.get("date"))
        if not DATE_PATTERN.match(raw_date) or not _is_iso_date(raw_date):
            report.add_error(
                f"users[{index}].checkins[{record_index}]：日期 {raw_date!r} 非法，已跳过",
            )
            continue

        streak_days = _positive_int(record.get("streak_days"), default=0)
        if streak_days <= 0:
            report.add_error(
                f"users[{index}].checkins[{record_index}]：streak_days 缺失或非法，"
                "按 0 计算（只补基础奖）",
            )
        reward = (
            backfill_reward_for_record(settings.coins, streak_days)
            if backfill_coins
            else None
        )
        coins_gain = 0 if reward is None else reward.total

        cursor = await connection.execute(
            """
            INSERT INTO checkins (
                player_pk, checkin_date, streak_days, coins_gain, source, created_at
            ) VALUES (?, ?, ?, ?, 'import', ?)
            ON CONFLICT(player_pk, checkin_date) DO NOTHING
            """,
            (
                player.id,
                raw_date,
                streak_days,
                coins_gain,
                _text(record.get("created_at")) or f"{raw_date}T00:00:00",
            ),
        )
        inserted = cursor.rowcount == 1
        await cursor.close()
        if inserted:
            report.checkins_inserted += 1
            coins_granted += coins_gain
        else:
            report.checkins_skipped += 1

    if coins_granted:
        report.coins_granted += coins_granted
        report.coins_players += 1
        await repository.apply_coin_delta_in_db(
            connection,
            player_pk=player.id,
            delta=coins_granted,
            reason="import",
            ref=f"legacy:{identity.user_id}",
        )

    await repository.refresh_aggregates_in_db(
        connection,
        player_pk=player.id,
    )
    refreshed = await repository.reload_in_db(connection, player.id)
    if updated or (player.last_checkin_date != refreshed.last_checkin_date):
        report.players_updated += 1


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _is_iso_date(text: str) -> bool:
    try:
        return date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False
