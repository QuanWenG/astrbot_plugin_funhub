"""配置解析与 _conf_schema.json 的一致性。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from funhub import config

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

#: AstrBot ``DEFAULT_VALUE_MAP`` 支持的配置类型
SUPPORTED_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
    "dict",
}


def test_defaults_come_from_the_module_constants():
    settings = config.load_settings({})
    assert settings.access.group_whitelist == ()
    assert settings.checkin.timezone == config.DEFAULT_TIMEZONE
    assert settings.checkin.day_reset_hour == config.DEFAULT_DAY_RESET_HOUR
    assert settings.checkin.auto_checkin is True
    assert settings.coins.base_reward == config.DEFAULT_BASE_REWARD
    assert settings.coins.cycle_short_days == config.DEFAULT_CYCLE_SHORT_DAYS
    assert settings.coins.cycle_long_days == config.DEFAULT_CYCLE_LONG_DAYS
    assert settings.levels.max_level == config.DEFAULT_MAX_LEVEL
    assert settings.import_.backfill_coins is True
    assert settings.ui.ranking_size == config.DEFAULT_RANKING_SIZE
    assert settings.warnings == ()


def test_none_and_non_mapping_config_are_safe():
    assert config.load_settings(None).checkin.day_reset_hour == config.DEFAULT_DAY_RESET_HOUR
    assert config.load_settings("nonsense").ui.ranking_size == config.DEFAULT_RANKING_SIZE
    assert config.load_settings({"checkin": "oops"}).checkin.auto_checkin is True


def test_whitelist_accepts_a_bare_string():
    settings = config.load_settings({"access": {"group_whitelist": "123"}})
    assert settings.access.group_whitelist == ("123",)


def test_whitelist_drops_junk_entries():
    settings = config.load_settings({"access": {"group_whitelist": ["123", "", None, 5]}})
    assert settings.access.group_whitelist == ("123",)
    assert any("group_whitelist" in warning for warning in settings.warnings)


@pytest.mark.parametrize(
    ("payload", "section", "field", "expected", "warns"),
    [
        ({"checkin": {"day_reset_hour": 99}}, "checkin", "day_reset_hour", config.DEFAULT_DAY_RESET_HOUR, True),
        ({"checkin": {"day_reset_hour": "abc"}}, "checkin", "day_reset_hour", config.DEFAULT_DAY_RESET_HOUR, True),
        ({"checkin": {"day_reset_hour": True}}, "checkin", "day_reset_hour", config.DEFAULT_DAY_RESET_HOUR, True),
        ({"checkin": {"day_reset_hour": "8"}}, "checkin", "day_reset_hour", 8, False),
        ({"coins": {"base_reward": -5}}, "coins", "base_reward", config.DEFAULT_BASE_REWARD, True),
        ({"coins": {"random_bonus_max": "12"}}, "coins", "random_bonus_max", 12, False),
        ({"ui": {"ranking_size": 9999}}, "ui", "ranking_size", config.DEFAULT_RANKING_SIZE, True),
        ({"levels": {"max_level": 0}}, "levels", "max_level", config.DEFAULT_MAX_LEVEL, True),
        ({"import": {"max_file_mb": 100000}}, "import_", "max_file_mb", config.DEFAULT_IMPORT_MAX_FILE_MB, True),
    ],
)
def test_bad_numbers_fall_back_with_a_warning(
    payload: dict,
    section: str,
    field: str,
    expected: int,
    warns: bool,
):
    settings = config.load_settings(payload)
    assert getattr(getattr(settings, section), field) == expected
    assert bool(settings.warnings) is warns, settings.warnings


def test_boolean_strings_are_understood():
    settings = config.load_settings({"checkin": {"auto_checkin": "false"}})
    assert settings.checkin.auto_checkin is False
    assert settings.warnings == ()


def test_bad_boolean_falls_back():
    settings = config.load_settings({"checkin": {"auto_checkin": "maybe"}})
    assert settings.checkin.auto_checkin is True
    assert any("auto_checkin" in warning for warning in settings.warnings)


def test_short_cycle_must_be_shorter_than_long_cycle():
    settings = config.load_settings(
        {"coins": {"cycle_short_days": 30, "cycle_long_days": 7}},
    )
    assert settings.coins.cycle_short_days == config.DEFAULT_CYCLE_SHORT_DAYS
    assert settings.coins.cycle_long_days == config.DEFAULT_CYCLE_LONG_DAYS
    assert any("cycle_short_days" in warning for warning in settings.warnings)


@pytest.mark.parametrize("mode", config.AUTO_CHECKIN_REPLY_MODES)
def test_auto_checkin_reply_accepts_every_documented_mode(mode: str):
    settings = config.load_settings({"checkin": {"auto_checkin_reply": mode}})
    assert settings.checkin.auto_checkin_reply == mode
    assert settings.warnings == ()


@pytest.mark.parametrize("raw", ["ALWAYS", "noisy", "", None, 1, True])
def test_auto_checkin_reply_falls_back_on_junk(raw):
    settings = config.load_settings({"checkin": {"auto_checkin_reply": raw}})
    assert settings.checkin.auto_checkin_reply == config.DEFAULT_AUTO_CHECKIN_REPLY
    assert any("auto_checkin_reply" in warning for warning in settings.warnings)


def test_cycles_property_is_short_first():
    settings = config.load_settings({})
    assert settings.coins.cycles == (
        (config.DEFAULT_CYCLE_SHORT_DAYS, config.DEFAULT_CYCLE_SHORT_BONUS),
        (config.DEFAULT_CYCLE_LONG_DAYS, config.DEFAULT_CYCLE_LONG_BONUS),
    )


def test_import_settings_expose_byte_limit():
    settings = config.load_settings({"import": {"max_file_mb": 3}})
    assert settings.import_.max_file_bytes == 3 * 1024 * 1024


# ---------------------------------------------------------------------------
# _conf_schema.json
# ---------------------------------------------------------------------------


def test_schema_parses_and_uses_supported_types():
    assert SCHEMA, "配置 schema 不该为空"
    for section, meta in SCHEMA.items():
        assert meta["type"] == "object", section
        assert meta.get("description"), section
        for key, item in meta["items"].items():
            assert item["type"] in SUPPORTED_TYPES, f"{section}.{key}"
            assert item.get("description"), f"{section}.{key}"
            if item["type"] == "list":
                assert item["items"] == {"type": "string"}, f"{section}.{key}"
            if "slider" in item:
                assert set(item["slider"]) >= {"min", "max", "step"}, f"{section}.{key}"


def test_schema_keys_match_the_runtime_settings():
    settings = config.load_settings({})
    sections = {
        "access": settings.access,
        "checkin": settings.checkin,
        "coins": settings.coins,
        "levels": settings.levels,
        "import": settings.import_,
        "ui": settings.ui,
        "duel": settings.duel,
        "roulette": settings.roulette,
    }
    for section, meta in SCHEMA.items():
        expected = {field.name for field in dataclasses.fields(sections[section])}
        assert set(meta["items"]) == expected, section


@pytest.mark.parametrize(
    ("section", "key", "expected"),
    [
        ("access", "group_whitelist", list(config.DEFAULT_GROUP_WHITELIST)),
        ("checkin", "timezone", config.DEFAULT_TIMEZONE),
        ("checkin", "day_reset_hour", config.DEFAULT_DAY_RESET_HOUR),
        ("checkin", "auto_checkin", config.DEFAULT_AUTO_CHECKIN),
        (
            "checkin",
            "auto_checkin_reply",
            config.DEFAULT_AUTO_CHECKIN_REPLY,
        ),
        ("coins", "base_reward", config.DEFAULT_BASE_REWARD),
        ("coins", "random_bonus_max", config.DEFAULT_RANDOM_BONUS_MAX),
        ("coins", "streak_bonus_per_day", config.DEFAULT_STREAK_BONUS_PER_DAY),
        ("coins", "streak_bonus_cap_days", config.DEFAULT_STREAK_BONUS_CAP_DAYS),
        ("coins", "cycle_short_days", config.DEFAULT_CYCLE_SHORT_DAYS),
        ("coins", "cycle_short_bonus", config.DEFAULT_CYCLE_SHORT_BONUS),
        ("coins", "cycle_long_days", config.DEFAULT_CYCLE_LONG_DAYS),
        ("coins", "cycle_long_bonus", config.DEFAULT_CYCLE_LONG_BONUS),
        ("coins", "first_checkin_bonus", config.DEFAULT_FIRST_CHECKIN_BONUS),
        ("levels", "max_level", config.DEFAULT_MAX_LEVEL),
        ("import", "default_platform", config.DEFAULT_IMPORT_PLATFORM),
        ("import", "backfill_coins", config.DEFAULT_IMPORT_BACKFILL_COINS),
        ("import", "max_file_mb", config.DEFAULT_IMPORT_MAX_FILE_MB),
        ("ui", "ranking_size", config.DEFAULT_RANKING_SIZE),
        ("duel", "enabled", config.DEFAULT_DUEL_ENABLED),
        ("duel", "dice_max", config.DEFAULT_DUEL_DICE_MAX),
        ("duel", "stake_max", config.DEFAULT_DUEL_STAKE_MAX),
        ("duel", "mute_seconds", config.DEFAULT_DUEL_MUTE_SECONDS),
        ("duel", "rich_step_coins", config.DEFAULT_DUEL_RICH_STEP_COINS),
        ("duel", "rich_step_percent", config.DEFAULT_DUEL_RICH_STEP_PERCENT),
        ("duel", "rich_cap_percent", config.DEFAULT_DUEL_RICH_CAP_PERCENT),
        ("duel", "admin_percent", config.DEFAULT_DUEL_ADMIN_PERCENT),
        ("roulette", "enabled", config.DEFAULT_ROULETTE_ENABLED),
        ("roulette", "chambers", config.DEFAULT_ROULETTE_CHAMBERS),
        ("roulette", "safe_reward", config.DEFAULT_ROULETTE_SAFE_REWARD),
        ("roulette", "safe_reward_step", config.DEFAULT_ROULETTE_SAFE_REWARD_STEP),
        ("roulette", "hit_penalty", config.DEFAULT_ROULETTE_HIT_PENALTY),
        ("roulette", "share_percent", config.DEFAULT_ROULETTE_SHARE_PERCENT),
        ("roulette", "misfire_percent", config.DEFAULT_ROULETTE_MISFIRE_PERCENT),
        ("roulette", "misfire_reward", config.DEFAULT_ROULETTE_MISFIRE_REWARD),
        ("roulette", "mute_seconds", config.DEFAULT_ROULETTE_MUTE_SECONDS),
        ("roulette", "timeout_seconds", config.DEFAULT_ROULETTE_TIMEOUT_SECONDS),
        ("roulette", "rescue_fail_percent", config.DEFAULT_ROULETTE_RESCUE_FAIL_PERCENT),
        ("roulette", "judgment_seconds", config.DEFAULT_ROULETTE_JUDGMENT_SECONDS),
        ("roulette", "judgment_fail_percent", config.DEFAULT_ROULETTE_JUDGMENT_FAIL_PERCENT),
        (
            "roulette",
            "judgment_backfire_percent",
            config.DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_PERCENT,
        ),
    ],
)
def test_schema_defaults_match_code_defaults(section: str, key: str, expected):
    assert SCHEMA[section]["items"][key]["default"] == expected


# ---------------------------------------------------------------------------
# 旧默认值升级：配置是存过盘的，改默认值不会自动生效
# ---------------------------------------------------------------------------


def test_superseded_defaults_are_upgraded():
    raw = {
        "access": {"group_whitelist": ["100"]},
        "roulette": {"safe_reward": 100, "share_percent": 50, "mute_seconds": 600},
        "duel": {"admin_percent": 50},
    }
    upgraded, changes = config.migrate_superseded_defaults(raw)

    assert upgraded["roulette"] == {
        "safe_reward": 50,
        "share_percent": 100,
        "mute_seconds": 60,
    }
    assert upgraded["duel"]["admin_percent"] == 20
    assert upgraded["access"] == {"group_whitelist": ["100"]}
    assert len(changes) == 4
    assert "roulette.safe_reward 100 → 50" in changes[0]


def test_upgrade_never_touches_user_tuned_values():
    raw = {
        "roulette": {"safe_reward": 250, "share_percent": 0, "mute_seconds": 600},
        "duel": {"admin_percent": 35},
    }
    upgraded, changes = config.migrate_superseded_defaults(raw)

    assert upgraded["roulette"]["safe_reward"] == 250
    assert upgraded["roulette"]["share_percent"] == 0
    assert upgraded["duel"]["admin_percent"] == 35
    # 只有 600（旧默认值）那一项被升级
    assert upgraded["roulette"]["mute_seconds"] == 60
    assert changes == ["roulette.mute_seconds 600 → 60"]


def test_misfire_percent_default_is_upgraded_too():
    """炸膛概率默认值从 12.5% 调到 25% 时，老配置也要跟上。"""
    upgraded, changes = config.migrate_superseded_defaults(
        {"roulette": {"misfire_percent": 12.5}},
    )

    assert upgraded["roulette"]["misfire_percent"] == 25.0
    assert changes == ["roulette.misfire_percent 12.5 → 25"]
    # 用户自己挑过的概率不动
    kept, none = config.migrate_superseded_defaults({"roulette": {"misfire_percent": 40}})
    assert kept["roulette"]["misfire_percent"] == 40
    assert none == []


def test_upgrade_ignores_junk_and_missing_sections():
    raw = {"roulette": {"safe_reward": "100", "mute_seconds": True}}
    upgraded, changes = config.migrate_superseded_defaults(raw)

    assert upgraded == raw  # 字符串 / 布尔不猜，交给 load_settings 兜底
    assert changes == []

    assert config.migrate_superseded_defaults({}) == ({}, [])


def test_upgrade_does_not_mutate_the_input():
    raw = {"roulette": {"safe_reward": 100}}
    config.migrate_superseded_defaults(raw)
    assert raw == {"roulette": {"safe_reward": 100}}


def test_upgraded_config_still_loads_into_settings():
    raw = {
        "roulette": {"safe_reward": 100, "mute_seconds": 600, "share_percent": 50},
    }
    upgraded, _ = config.migrate_superseded_defaults(raw)
    settings = config.load_settings(upgraded)

    assert settings.roulette.safe_reward == 50
    assert settings.roulette.mute_seconds == 60
    assert settings.roulette.share_percent == 100
    assert settings.warnings == ()  # 升级后的值都是合法值，不该有 warning
