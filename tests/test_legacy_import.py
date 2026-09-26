"""老插件数据导入：映射、补发、幂等、坏记录、连签续接。

补发额的期望值口径在 ``coins.py``：随机奖取期望值 ``random_bonus_max // 2``。
按默认配置（基础 100 / 随机期望 25 / 连签 10 每天封顶 15 天 / 7 天周期 300 / 30 天周期 1500）::

    streak 1  → 100 + 25            = 125
    streak 2  → 100 + 25 + 10       = 135
    streak 7  → 100 + 25 + 60 + 300 = 485
    streak 30 → 100 + 25 + 150 + 1500 = 1775

样例文件 ``tests/fixtures/legacy_export_sample.json`` 就是按这几档设计的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from funhub.config import load_settings
from funhub.legacy import LegacyImportError, import_export, parse_export, ImportReport

from .conftest import identity

HK = timezone(timedelta(hours=8))

#: 样例文件里每个用户应得的补发额
EXPECTED_USER_200 = 125 + 135 + 485
EXPECTED_USER_201 = 1775
EXPECTED_USER_300 = 125
EXPECTED_TOTAL = EXPECTED_USER_200 + EXPECTED_USER_201 + EXPECTED_USER_300


async def test_import_maps_level_records_and_streak(database, settings, legacy_payload):
    report = await import_export(
        database,
        legacy_payload,
        settings,
        filename="legacy.json",
    )

    assert report.files == ["legacy.json"]
    assert report.players_new == 3
    assert report.checkins_inserted == 5
    assert report.checkins_skipped == 1  # 同一天写了两遍的那条
    assert report.coins_granted == EXPECTED_TOTAL
    assert report.coins_players == 3
    assert report.dry_run is False

    player = await database.fetchone(
        "SELECT * FROM players WHERE group_id = '100' AND user_id = '200'",
    )
    assert int(player["level"]) == 12
    assert player["nickname"] == "龟甲"
    assert int(player["total_checkins"]) == 3
    assert int(player["streak_days"]) == 7
    assert int(player["best_streak"]) == 7
    assert player["last_checkin_date"] == "2026-01-03"
    assert int(player["coins"]) == EXPECTED_USER_200
    assert int(player["total_coins"]) == EXPECTED_USER_200


async def test_level_is_clamped_and_never_lowered(database, settings, legacy_payload):
    await import_export(database, legacy_payload, settings)
    row = await database.fetchone(
        "SELECT level, nickname FROM players WHERE user_id = '201'",
    )
    assert int(row["level"]) == settings.levels.max_level  # 150 → 100
    assert row["nickname"] == "201"  # 空昵称回落到 user_id

    # 再次导入更低等级的同一份数据不会把等级压下去
    lower = {
        "users": [
            {
                "platform": "aiocqhttp",
                "group_id": "100",
                "user_id": "201",
                "level": 3,
                "checkins": [],
            },
        ],
    }
    await import_export(database, lower, settings)
    row = await database.fetchone("SELECT level FROM players WHERE user_id = '201'")
    assert int(row["level"]) == settings.levels.max_level


async def test_second_import_changes_nothing(database, settings, legacy_payload):
    first = await import_export(database, legacy_payload, settings)
    second = await import_export(database, legacy_payload, settings)

    assert first.coins_granted == EXPECTED_TOTAL
    assert second.coins_granted == 0
    assert second.coins_players == 0
    assert second.checkins_inserted == 0
    # 第一次插入的 + 第一次因同一天重复而跳过的 = 第二次全部命中已存在
    assert second.checkins_skipped == first.checkins_inserted + first.checkins_skipped
    assert second.players_new == 0
    assert second.players_updated == 0


async def test_dry_run_reports_but_does_not_write(database, settings, legacy_payload):
    preview = await import_export(database, legacy_payload, settings, dry_run=True)

    assert preview.dry_run is True
    assert preview.coins_granted == EXPECTED_TOTAL
    assert preview.checkins_inserted == 5

    players = await database.fetchone("SELECT COUNT(*) AS total FROM players")
    checkins = await database.fetchone("SELECT COUNT(*) AS total FROM checkins")
    assert int(players["total"]) == 0
    assert int(checkins["total"]) == 0

    # 预览之后再真跑一次，结果与预览一致
    real = await import_export(database, legacy_payload, settings)
    assert real.coins_granted == preview.coins_granted
    assert real.checkins_inserted == preview.checkins_inserted


async def test_no_backfill_keeps_records_without_paying(database, settings, legacy_payload):
    report = await import_export(
        database,
        legacy_payload,
        settings,
        backfill_coins=False,
    )
    assert report.checkins_inserted == 5
    assert report.coins_granted == 0
    row = await database.fetchone("SELECT coins, total_coins FROM players WHERE user_id = '200'")
    assert int(row["coins"]) == 0
    assert int(row["total_coins"]) == 0
    granted = await database.fetchone("SELECT COALESCE(SUM(coins_gain), 0) AS g FROM checkins")
    assert int(granted["g"]) == 0


async def test_bad_records_are_reported_and_skipped(database, settings, legacy_payload):
    report = await import_export(database, legacy_payload, settings)
    joined = "\n".join(report.errors)
    assert "缺少 user_id" in joined
    assert "2025-13-40" in joined
    assert "不是日期" in joined
    assert "不是对象" in joined
    assert "streak_days 缺失或非法" in joined

    # 坏记录不影响同一用户的好记录入库
    row = await database.fetchone("SELECT total_checkins FROM players WHERE user_id = '300'")
    assert int(row["total_checkins"]) == 1


async def test_streak_continues_after_import(database, settings, legacy_payload, make_service):
    await import_export(database, legacy_payload, settings)
    service = make_service(0)
    outcome = await service.check_in(
        identity(user_id="200", nickname="龟甲"),
        now=datetime(2026, 1, 4, 12, 0, tzinfo=HK),
    )
    assert outcome.streak_days == 8  # 老数据最后一天连签 7 天，接上
    assert outcome.reward is not None
    assert outcome.reward.first_bonus == 0  # 老用户没有首次签到礼


async def test_streak_breaks_when_history_is_stale(database, settings, legacy_payload, make_service):
    await import_export(database, legacy_payload, settings)
    service = make_service(0)
    outcome = await service.check_in(
        identity(user_id="200", nickname="龟甲"),
        now=datetime(2026, 2, 1, 12, 0, tzinfo=HK),
    )
    assert outcome.streak_days == 1


async def test_coins_invariant_holds_after_import(database, settings, legacy_payload):
    await import_export(database, legacy_payload, settings)
    rows = await database.fetchall(
        """
        SELECT p.user_id, p.total_coins,
               COALESCE(SUM(c.coins_gain), 0) AS granted
        FROM players p LEFT JOIN checkins c ON c.player_pk = p.id
        GROUP BY p.id
        """,
    )
    assert rows
    for row in rows:
        assert int(row["total_coins"]) == int(row["granted"]), row["user_id"]


async def test_missing_platform_uses_default(database, settings):
    payload = {
        "users": [
            {
                "group_id": "100",
                "user_id": "777",
                "nickname": "无平台",
                "level": 4,
                "checkins": [{"date": "2026-01-01", "streak_days": 1}],
            },
        ],
    }
    await import_export(database, payload, settings)
    row = await database.fetchone("SELECT platform, coins FROM players WHERE user_id = '777'")
    assert row["platform"] == settings.import_.default_platform
    assert int(row["coins"]) == 125


async def test_platform_override_is_honoured(database):
    settings = load_settings({"import": {"default_platform": "napcat"}})
    payload = {"users": [{"group_id": "1", "user_id": "2", "level": 1}]}
    await import_export(database, payload, settings)
    row = await database.fetchone("SELECT platform FROM players WHERE user_id = '2'")
    assert row["platform"] == "napcat"


def test_parse_export_rejects_wrong_shapes():
    report = ImportReport()
    with pytest.raises(LegacyImportError):
        parse_export([], report=report)
    with pytest.raises(LegacyImportError):
        parse_export({"users": "nope"}, report=report)


def test_parse_export_warns_on_schema_drift():
    report = ImportReport()
    users = parse_export(
        {"schema_version": 9, "source": "something-else", "users": [{"user_id": "1"}]},
        report=report,
    )
    assert len(users) == 1
    assert any("schema_version" in warning for warning in report.warnings)
    assert any("source" in warning for warning in report.warnings)


def test_parse_export_drops_non_dict_entries():
    report = ImportReport()
    users = parse_export({"users": [{"user_id": "1"}, "垃圾", 42]}, report=report)
    assert users == [{"user_id": "1"}]


async def test_import_of_empty_users_is_a_no_op(database, settings):
    report = await import_export(database, {"users": []}, settings)
    assert report.players_new == 0
    assert report.coins_granted == 0
    assert report.errors == []
