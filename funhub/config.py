"""插件配置的解析与兜底。

AstrBot 会把 ``_conf_schema.json`` 的默认值与用户在 WebUI 的改动合成一个
``dict`` 传给插件。用户可以在 WebUI 里把任何字段改成任何东西（清空输入框、
填负数、填字符串），所以这里的职责是：**把任意输入收敛成一份可用的 Settings**，
坏值一律回落到默认值并记一条 warning，绝不让签到因为配置报错。

另外，AstrBot 的配置是**存过盘**的：改了默认值以后，用户配置里那份旧值不会自己
跟着变（"第一枪怎么还是 100"就是这么来的）。:func:`migrate_superseded_defaults`
负责把这些"还停在旧默认值"的项升到新默认值 —— 只动恰好等于旧默认值的项，
用户自己调过的数值一律保留；调用方（``main.py``）用数据库 meta 保证只跑一次。

本模块不 import astrbot，测试直接传普通 ``dict`` 即可。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 默认值。与 _conf_schema.json 的 default 必须保持一致（tests/test_config.py 校验）。
# ---------------------------------------------------------------------------

DEFAULT_GROUP_WHITELIST: tuple[str, ...] = ()
DEFAULT_TIMEZONE = "Asia/Hong_Kong"
DEFAULT_DAY_RESET_HOUR = 4
DEFAULT_AUTO_CHECKIN = True
DEFAULT_AUTO_CHECKIN_REPLY = "always"
#: 自动签到的出声策略：
#:   always —— 每次都回签到卡（与老插件一致，最容易确认它在工作）
#:   cycle  —— 只在命中 7 / 30 天周期奖励时出声
#:   silent —— 完全静默，只入库
AUTO_CHECKIN_REPLY_MODES = ("always", "cycle", "silent")

DEFAULT_BASE_REWARD = 100
DEFAULT_RANDOM_BONUS_MAX = 50
DEFAULT_STREAK_BONUS_PER_DAY = 10
DEFAULT_STREAK_BONUS_CAP_DAYS = 15
DEFAULT_CYCLE_SHORT_DAYS = 7
DEFAULT_CYCLE_SHORT_BONUS = 300
DEFAULT_CYCLE_LONG_DAYS = 30
DEFAULT_CYCLE_LONG_BONUS = 1500
DEFAULT_FIRST_CHECKIN_BONUS = 200

DEFAULT_MAX_LEVEL = 100
DEFAULT_IMPORT_PLATFORM = "aiocqhttp"
DEFAULT_IMPORT_BACKFILL_COINS = True
DEFAULT_IMPORT_MAX_FILE_MB = 20
DEFAULT_RANKING_SIZE = 10

#: 决斗（一轮定胜负，无冷却）
DEFAULT_DUEL_ENABLED = True
DEFAULT_DUEL_DICE_MAX = 100
DEFAULT_DUEL_STAKE_MAX = 500
DEFAULT_DUEL_MUTE_SECONDS = 60
#: 败者存款每比胜者多 rich_step_coins，赔付多 rich_step_percent%，最多 rich_cap_percent%
DEFAULT_DUEL_RICH_STEP_COINS = 500
DEFAULT_DUEL_RICH_STEP_PERCENT = 10
DEFAULT_DUEL_RICH_CAP_PERCENT = 50
#: 败者是群主/管理员时（平台不给禁言）额外多赔的比例。
#: 「+0.2 倍」是在普通人算出来的倍率上**加算**，不是固定值。
DEFAULT_DUEL_ADMIN_PERCENT = 20

#: 轮盘（6 个弹槽 1 颗子弹，谁都可能扣扳机）
DEFAULT_ROULETTE_ENABLED = True
DEFAULT_ROULETTE_CHAMBERS = 6
#: 第一枪的空枪奖励，之后每多开一枪再加 safe_reward_step（50 / 100 / 150 …）
DEFAULT_ROULETTE_SAFE_REWARD = 50
DEFAULT_ROULETTE_SAFE_REWARD_STEP = 50
DEFAULT_ROULETTE_HIT_PENALTY = 500
DEFAULT_ROULETTE_SHARE_PERCENT = 100
#: 抽到子弹的那一枪（子弹在第几个弹槽就第几枪）有 ``misfire_percent``% 概率炸膛：
#: 开枪的人不中弹，反而拿 ``misfire_reward``，本局就此结束
DEFAULT_ROULETTE_MISFIRE_PERCENT = 25.0
DEFAULT_ROULETTE_MISFIRE_REWARD = 500
#: 醉酒时中弹会连带几个同局开过枪的人一起受罚（原版"醉酒随机命中多人"）
DEFAULT_ROULETTE_DRUNK_EXTRA_MAX = 2
#: 醉酒时"走火"的概率：醉了是前提，走火了才会连带别人
DEFAULT_ROULETTE_DRUNK_EXTRA_PERCENT = 25.0
DEFAULT_ROULETTE_MUTE_SECONDS = 60
DEFAULT_ROULETTE_TIMEOUT_SECONDS = 300
#: 「/救一下」也有概率失败（救援本身的失败，与开枪无关）
DEFAULT_ROULETTE_RESCUE_FAIL_PERCENT = 12.5
#: 「/补一枪」：给中弹被禁言的人追加一次禁言（照搬 Pallas-Bot 的 judgment，
#: 去掉踢人与醉酒联动，只留"追加禁言 + 卡壳 / 反噬"）
DEFAULT_ROULETTE_JUDGMENT_SECONDS = 60
DEFAULT_ROULETTE_JUDGMENT_FAIL_PERCENT = 12.5
DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_PERCENT = 12.5
#: 同一个目标被"补一枪 / 救一下"之后，多久内不能再挨另一个操作
#: （防止补完就救、救完就补地来回折腾）
DEFAULT_ROULETTE_ACTION_COOLDOWN_SECONDS = 60
#: 反噬是否只在"群里喝醉"时判定（原版语义）。喝酒状态来自别的插件
#: （默认 astrbot_plugin_repeater），读不到时视为清醒。
DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_DRUNK_ONLY = True

#: 配置上限，避免用户把 slider 之外的值写进 schema 后打爆内存或响应体。
MAX_RANKING_SIZE = 50
MAX_IMPORT_FILE_MB = 512

#: 配置结构版本：写进数据库 meta（``config_schema_version``），保证升级只跑一次。
#: 每次往 :data:`SUPERSEDED_DEFAULTS` 加条目都要 +1，否则老配置不会重新走一遍。
CONFIG_SCHEMA_VERSION = 4

#: 被取代的默认值：(分区, 键, 旧默认值, 新默认值)。
#:
#: 只在这些项**恰好还等于旧默认值**时才改写 —— 那是"用户没动过、只是配置存得早"，
#: 用户自己调过的数值（不等于旧默认值）一律不碰。顺序即升级说明的顺序。
SUPERSEDED_DEFAULTS: tuple[tuple[str, str, float, float], ...] = (
    # 空枪奖励：每次固定 100 → 第一枪 50、之后每枪再多 50
    ("roulette", "safe_reward", 100, DEFAULT_ROULETTE_SAFE_REWARD),
    # 中弹赔付：扣款分一半给参与者 → 全额赔给开过枪的人
    ("roulette", "share_percent", 50, DEFAULT_ROULETTE_SHARE_PERCENT),
    # 中弹禁言：10 分钟 → 1 分钟
    ("roulette", "mute_seconds", 600, DEFAULT_ROULETTE_MUTE_SECONDS),
    # 决斗管理员加成：+50% → 倍率再加 0.2
    ("duel", "admin_percent", 50, DEFAULT_DUEL_ADMIN_PERCENT),
    # 炸膛概率：12.5% → 25%
    ("roulette", "misfire_percent", 12.5, DEFAULT_ROULETTE_MISFIRE_PERCENT),
    # 醉酒走火概率：100%（醉了必走火）→ 25%
    (
        "roulette",
        "drunk_extra_percent",
        100,
        DEFAULT_ROULETTE_DRUNK_EXTRA_PERCENT,
    ),
)


def _number(value: float) -> str:
    return f"{value:g}"


def migrate_superseded_defaults(
    raw_config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """把"还停在旧默认值"的配置项升到新默认值。

    返回 ``(升级后的配置, 变更说明)``；没有任何改动时说明为空列表。
    只认数字，字符串 / 布尔一律跳过（那些本来就会被 :func:`load_settings` 兜底）。
    """
    updated = copy.deepcopy(dict(raw_config))
    changes: list[str] = []
    for section, key, old, new in SUPERSEDED_DEFAULTS:
        block = updated.get(section)
        if not isinstance(block, dict) or key not in block:
            continue
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if float(value) == float(old) and float(old) != float(new):
            block[key] = new
            changes.append(f"{section}.{key} {_number(old)} → {_number(new)}")
    return updated, changes


@dataclass(frozen=True, slots=True)
class AccessSettings:
    """白名单。留空 = 所有群都不生效（与老插件一致）。"""

    group_whitelist: tuple[str, ...] = DEFAULT_GROUP_WHITELIST


@dataclass(frozen=True, slots=True)
class CheckinSettings:
    timezone: str = DEFAULT_TIMEZONE
    day_reset_hour: int = DEFAULT_DAY_RESET_HOUR
    auto_checkin: bool = DEFAULT_AUTO_CHECKIN
    auto_checkin_reply: str = DEFAULT_AUTO_CHECKIN_REPLY


@dataclass(frozen=True, slots=True)
class CoinSettings:
    """龟龟币奖励参数。100 币 ≈ 1 元只用于标定量级，不对玩家展示。"""

    base_reward: int = DEFAULT_BASE_REWARD
    random_bonus_max: int = DEFAULT_RANDOM_BONUS_MAX
    streak_bonus_per_day: int = DEFAULT_STREAK_BONUS_PER_DAY
    streak_bonus_cap_days: int = DEFAULT_STREAK_BONUS_CAP_DAYS
    cycle_short_days: int = DEFAULT_CYCLE_SHORT_DAYS
    cycle_short_bonus: int = DEFAULT_CYCLE_SHORT_BONUS
    cycle_long_days: int = DEFAULT_CYCLE_LONG_DAYS
    cycle_long_bonus: int = DEFAULT_CYCLE_LONG_BONUS
    first_checkin_bonus: int = DEFAULT_FIRST_CHECKIN_BONUS

    @property
    def cycles(self) -> tuple[tuple[int, int], ...]:
        """(周期天数, 周期奖励)，短周期在前，便于稳定输出与断言。"""
        return (
            (self.cycle_short_days, self.cycle_short_bonus),
            (self.cycle_long_days, self.cycle_long_bonus),
        )


@dataclass(frozen=True, slots=True)
class LevelSettings:
    max_level: int = DEFAULT_MAX_LEVEL


@dataclass(frozen=True, slots=True)
class ImportSettings:
    default_platform: str = DEFAULT_IMPORT_PLATFORM
    backfill_coins: bool = DEFAULT_IMPORT_BACKFILL_COINS
    max_file_mb: int = DEFAULT_IMPORT_MAX_FILE_MB

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


@dataclass(frozen=True, slots=True)
class UiSettings:
    ranking_size: int = DEFAULT_RANKING_SIZE


@dataclass(frozen=True, slots=True)
class DuelSettings:
    """决斗奖惩。

    赔付 = 随机基础额 × 倍率，倍率由两部分加算：

    * **存款碾压**：败者每比胜者多 ``rich_step_coins`` 币，多赔 ``rich_step_percent``%，
      最多 ``rich_cap_percent``%（普通人因此最多 1.5 倍）；
    * **管理员败北**：败者是群主/管理员时（平台不允许禁言他们），在普通人算出来的
      倍率上**再加 0.2 倍**（``admin_percent`` 默认 20，是个加数、不是固定倍率），
      所以管理员最多 1.7 倍。

    实际扣款仍按"尽所能"：余额不够就全给。
    """

    enabled: bool = DEFAULT_DUEL_ENABLED
    dice_max: int = DEFAULT_DUEL_DICE_MAX
    stake_max: int = DEFAULT_DUEL_STAKE_MAX
    mute_seconds: int = DEFAULT_DUEL_MUTE_SECONDS
    rich_step_coins: int = DEFAULT_DUEL_RICH_STEP_COINS
    rich_step_percent: int = DEFAULT_DUEL_RICH_STEP_PERCENT
    rich_cap_percent: int = DEFAULT_DUEL_RICH_CAP_PERCENT
    admin_percent: int = DEFAULT_DUEL_ADMIN_PERCENT


@dataclass(frozen=True, slots=True)
class RouletteSettings:
    """龟龟轮盘。

    一局若干弹槽、一颗子弹，弹槽位置**开局就定好**（第几枪响是确定的），所以
    连开就是赌命。空枪发奖（第一枪 ``safe_reward``，之后每枪再加 ``safe_reward_step``）；
    抽到子弹的那一枪还有 ``misfire_percent``% 概率炸膛 —— 开枪的人不中弹，反而拿
    ``misfire_reward``，本局结束；否则中弹扣 ``hit_penalty``（全额平分给本局其他开过枪
    的人）并禁言。不能禁言的角色（群主/管理员）只扣币。
    """

    enabled: bool = DEFAULT_ROULETTE_ENABLED
    chambers: int = DEFAULT_ROULETTE_CHAMBERS
    safe_reward: int = DEFAULT_ROULETTE_SAFE_REWARD
    safe_reward_step: int = DEFAULT_ROULETTE_SAFE_REWARD_STEP
    hit_penalty: int = DEFAULT_ROULETTE_HIT_PENALTY
    share_percent: int = DEFAULT_ROULETTE_SHARE_PERCENT
    misfire_percent: float = DEFAULT_ROULETTE_MISFIRE_PERCENT
    misfire_reward: int = DEFAULT_ROULETTE_MISFIRE_REWARD
    drunk_extra_max: int = DEFAULT_ROULETTE_DRUNK_EXTRA_MAX
    drunk_extra_percent: float = DEFAULT_ROULETTE_DRUNK_EXTRA_PERCENT
    mute_seconds: int = DEFAULT_ROULETTE_MUTE_SECONDS
    timeout_seconds: int = DEFAULT_ROULETTE_TIMEOUT_SECONDS
    rescue_fail_percent: float = DEFAULT_ROULETTE_RESCUE_FAIL_PERCENT
    judgment_seconds: int = DEFAULT_ROULETTE_JUDGMENT_SECONDS
    judgment_fail_percent: float = DEFAULT_ROULETTE_JUDGMENT_FAIL_PERCENT
    judgment_backfire_percent: float = DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_PERCENT
    judgment_backfire_drunk_only: bool = DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_DRUNK_ONLY
    action_cooldown_seconds: int = DEFAULT_ROULETTE_ACTION_COOLDOWN_SECONDS


@dataclass(frozen=True, slots=True)
class Settings:
    access: AccessSettings = field(default_factory=AccessSettings)
    checkin: CheckinSettings = field(default_factory=CheckinSettings)
    coins: CoinSettings = field(default_factory=CoinSettings)
    levels: LevelSettings = field(default_factory=LevelSettings)
    import_: ImportSettings = field(default_factory=ImportSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    duel: DuelSettings = field(default_factory=DuelSettings)
    roulette: RouletteSettings = field(default_factory=RouletteSettings)
    #: 解析过程中发现的问题，由 main.py 用 AstrBot logger 输出。
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 取值辅助
# ---------------------------------------------------------------------------


def _section(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    return value if isinstance(value, Mapping) else {}


def _int(
    section: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    warnings: list[str],
    label: str,
) -> int:
    raw = section.get(key, default)
    if isinstance(raw, bool) or raw is None:
        warnings.append(f"{label} 需要整数，收到 {raw!r}，已回落默认值 {default}")
        return default
    try:
        value = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        warnings.append(f"{label} 需要整数，收到 {raw!r}，已回落默认值 {default}")
        return default
    if minimum is not None and value < minimum:
        warnings.append(f"{label} 不能小于 {minimum}，收到 {value}，已回落默认值 {default}")
        return default
    if maximum is not None and value > maximum:
        warnings.append(f"{label} 不能大于 {maximum}，收到 {value}，已回落默认值 {default}")
        return default
    return value


def _bool(
    section: Mapping[str, Any],
    key: str,
    default: bool,
    *,
    warnings: list[str],
    label: str,
) -> bool:
    raw = section.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    warnings.append(f"{label} 需要布尔值，收到 {raw!r}，已回落默认值 {default}")
    return default


def _str(
    section: Mapping[str, Any],
    key: str,
    default: str,
    *,
    warnings: list[str],
    label: str,
) -> str:
    raw = section.get(key, default)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    warnings.append(f"{label} 需要非空字符串，收到 {raw!r}，已回落默认值 {default!r}")
    return default


def _float(
    section: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    warnings: list[str],
    label: str,
) -> float:
    raw = section.get(key, default)
    if isinstance(raw, bool) or raw is None:
        warnings.append(f"{label} 需要数字，收到 {raw!r}，已回落默认值 {default}")
        return default
    try:
        value = float(str(raw).strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        warnings.append(f"{label} 需要数字，收到 {raw!r}，已回落默认值 {default}")
        return default
    if minimum is not None and value < minimum:
        warnings.append(f"{label} 不能小于 {minimum}，收到 {value}，已回落默认值 {default}")
        return default
    if maximum is not None and value > maximum:
        warnings.append(f"{label} 不能大于 {maximum}，收到 {value}，已回落默认值 {default}")
        return default
    return value


def _choice(
    section: Mapping[str, Any],
    key: str,
    default: str,
    *,
    choices: tuple[str, ...],
    warnings: list[str],
    label: str,
) -> str:
    """枚举选项。WebUI 的下拉框可能被写成任意值，非选项内的值一律回落默认。"""
    raw = section.get(key, default)
    if isinstance(raw, str) and raw.strip() in choices:
        return raw.strip()
    warnings.append(
        f"{label} 必须是 {' / '.join(choices)} 之一，收到 {raw!r}，已回落默认值 {default!r}",
    )
    return default


def _str_tuple(
    section: Mapping[str, Any],
    key: str,
    *,
    warnings: list[str],
    label: str,
) -> tuple[str, ...]:
    raw = section.get(key, ())
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        entries: tuple[Any, ...] = (raw,)
    elif isinstance(raw, Sequence):
        entries = tuple(raw)
    else:
        warnings.append(f"{label} 需要列表，收到 {raw!r}，已按空名单处理")
        return ()
    cleaned = tuple(
        item.strip() for item in entries if isinstance(item, str) and item.strip()
    )
    if len(cleaned) != len(entries):
        warnings.append(f"{label} 含非字符串或空项，已忽略这些项")
    return cleaned


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_settings(config: Mapping[str, Any] | None) -> Settings:
    """把任意配置映射收敛成 :class:`Settings`。"""
    raw_config: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
    warnings: list[str] = []

    access_raw = _section(raw_config, "access")
    checkin_raw = _section(raw_config, "checkin")
    coins_raw = _section(raw_config, "coins")
    levels_raw = _section(raw_config, "levels")
    import_raw = _section(raw_config, "import")
    ui_raw = _section(raw_config, "ui")

    access = AccessSettings(
        group_whitelist=_str_tuple(
            access_raw,
            "group_whitelist",
            warnings=warnings,
            label="access.group_whitelist",
        ),
    )

    checkin = CheckinSettings(
        timezone=_str(
            checkin_raw,
            "timezone",
            DEFAULT_TIMEZONE,
            warnings=warnings,
            label="checkin.timezone",
        ),
        day_reset_hour=_int(
            checkin_raw,
            "day_reset_hour",
            DEFAULT_DAY_RESET_HOUR,
            minimum=0,
            maximum=23,
            warnings=warnings,
            label="checkin.day_reset_hour",
        ),
        auto_checkin=_bool(
            checkin_raw,
            "auto_checkin",
            DEFAULT_AUTO_CHECKIN,
            warnings=warnings,
            label="checkin.auto_checkin",
        ),
        auto_checkin_reply=_choice(
            checkin_raw,
            "auto_checkin_reply",
            DEFAULT_AUTO_CHECKIN_REPLY,
            choices=AUTO_CHECKIN_REPLY_MODES,
            warnings=warnings,
            label="checkin.auto_checkin_reply",
        ),
    )

    coins = CoinSettings(
        base_reward=_int(
            coins_raw, "base_reward", DEFAULT_BASE_REWARD,
            minimum=0, maximum=1_000_000, warnings=warnings, label="coins.base_reward",
        ),
        random_bonus_max=_int(
            coins_raw, "random_bonus_max", DEFAULT_RANDOM_BONUS_MAX,
            minimum=0, maximum=1_000_000, warnings=warnings, label="coins.random_bonus_max",
        ),
        streak_bonus_per_day=_int(
            coins_raw, "streak_bonus_per_day", DEFAULT_STREAK_BONUS_PER_DAY,
            minimum=0, maximum=100_000, warnings=warnings,
            label="coins.streak_bonus_per_day",
        ),
        streak_bonus_cap_days=_int(
            coins_raw, "streak_bonus_cap_days", DEFAULT_STREAK_BONUS_CAP_DAYS,
            minimum=0, maximum=3650, warnings=warnings,
            label="coins.streak_bonus_cap_days",
        ),
        cycle_short_days=_int(
            coins_raw, "cycle_short_days", DEFAULT_CYCLE_SHORT_DAYS,
            minimum=1, maximum=3650, warnings=warnings, label="coins.cycle_short_days",
        ),
        cycle_short_bonus=_int(
            coins_raw, "cycle_short_bonus", DEFAULT_CYCLE_SHORT_BONUS,
            minimum=0, maximum=10_000_000, warnings=warnings,
            label="coins.cycle_short_bonus",
        ),
        cycle_long_days=_int(
            coins_raw, "cycle_long_days", DEFAULT_CYCLE_LONG_DAYS,
            minimum=1, maximum=3650, warnings=warnings, label="coins.cycle_long_days",
        ),
        cycle_long_bonus=_int(
            coins_raw, "cycle_long_bonus", DEFAULT_CYCLE_LONG_BONUS,
            minimum=0, maximum=10_000_000, warnings=warnings,
            label="coins.cycle_long_bonus",
        ),
        first_checkin_bonus=_int(
            coins_raw, "first_checkin_bonus", DEFAULT_FIRST_CHECKIN_BONUS,
            minimum=0, maximum=1_000_000, warnings=warnings,
            label="coins.first_checkin_bonus",
        ),
    )
    if coins.cycle_short_days >= coins.cycle_long_days:
        warnings.append(
            "coins.cycle_short_days 必须小于 coins.cycle_long_days"
            f"（当前 {coins.cycle_short_days} / {coins.cycle_long_days}），"
            "两个周期参数已回落默认值",
        )
        coins = CoinSettings(
            base_reward=coins.base_reward,
            random_bonus_max=coins.random_bonus_max,
            streak_bonus_per_day=coins.streak_bonus_per_day,
            streak_bonus_cap_days=coins.streak_bonus_cap_days,
            cycle_short_days=DEFAULT_CYCLE_SHORT_DAYS,
            cycle_short_bonus=coins.cycle_short_bonus,
            cycle_long_days=DEFAULT_CYCLE_LONG_DAYS,
            cycle_long_bonus=coins.cycle_long_bonus,
            first_checkin_bonus=coins.first_checkin_bonus,
        )

    levels = LevelSettings(
        max_level=_int(
            levels_raw, "max_level", DEFAULT_MAX_LEVEL,
            minimum=1, maximum=10_000, warnings=warnings, label="levels.max_level",
        ),
    )

    import_settings = ImportSettings(
        default_platform=_str(
            import_raw,
            "default_platform",
            DEFAULT_IMPORT_PLATFORM,
            warnings=warnings,
            label="import.default_platform",
        ),
        backfill_coins=_bool(
            import_raw,
            "backfill_coins",
            DEFAULT_IMPORT_BACKFILL_COINS,
            warnings=warnings,
            label="import.backfill_coins",
        ),
        max_file_mb=_int(
            import_raw, "max_file_mb", DEFAULT_IMPORT_MAX_FILE_MB,
            minimum=1, maximum=MAX_IMPORT_FILE_MB, warnings=warnings,
            label="import.max_file_mb",
        ),
    )

    ui = UiSettings(
        ranking_size=_int(
            ui_raw, "ranking_size", DEFAULT_RANKING_SIZE,
            minimum=1, maximum=MAX_RANKING_SIZE, warnings=warnings,
            label="ui.ranking_size",
        ),
    )

    duel_raw = _section(raw_config, "duel")
    duel = DuelSettings(
        enabled=_bool(
            duel_raw, "enabled", DEFAULT_DUEL_ENABLED,
            warnings=warnings, label="duel.enabled",
        ),
        dice_max=_int(
            duel_raw, "dice_max", DEFAULT_DUEL_DICE_MAX,
            minimum=1, maximum=1_000_000, warnings=warnings, label="duel.dice_max",
        ),
        stake_max=_int(
            duel_raw, "stake_max", DEFAULT_DUEL_STAKE_MAX,
            minimum=0, maximum=1_000_000, warnings=warnings, label="duel.stake_max",
        ),
        mute_seconds=_int(
            duel_raw, "mute_seconds", DEFAULT_DUEL_MUTE_SECONDS,
            minimum=1, maximum=2_592_000, warnings=warnings, label="duel.mute_seconds",
        ),
        rich_step_coins=_int(
            duel_raw, "rich_step_coins", DEFAULT_DUEL_RICH_STEP_COINS,
            minimum=1, maximum=10_000_000, warnings=warnings,
            label="duel.rich_step_coins",
        ),
        rich_step_percent=_int(
            duel_raw, "rich_step_percent", DEFAULT_DUEL_RICH_STEP_PERCENT,
            minimum=0, maximum=1000, warnings=warnings,
            label="duel.rich_step_percent",
        ),
        rich_cap_percent=_int(
            duel_raw, "rich_cap_percent", DEFAULT_DUEL_RICH_CAP_PERCENT,
            minimum=0, maximum=10_000, warnings=warnings,
            label="duel.rich_cap_percent",
        ),
        admin_percent=_int(
            duel_raw, "admin_percent", DEFAULT_DUEL_ADMIN_PERCENT,
            minimum=0, maximum=10_000, warnings=warnings,
            label="duel.admin_percent",
        ),
    )

    roulette_raw = _section(raw_config, "roulette")
    roulette = RouletteSettings(
        enabled=_bool(
            roulette_raw, "enabled", DEFAULT_ROULETTE_ENABLED,
            warnings=warnings, label="roulette.enabled",
        ),
        chambers=_int(
            roulette_raw, "chambers", DEFAULT_ROULETTE_CHAMBERS,
            minimum=1, maximum=100, warnings=warnings, label="roulette.chambers",
        ),
        safe_reward=_int(
            roulette_raw, "safe_reward", DEFAULT_ROULETTE_SAFE_REWARD,
            minimum=0, maximum=1_000_000, warnings=warnings,
            label="roulette.safe_reward",
        ),
        safe_reward_step=_int(
            roulette_raw, "safe_reward_step", DEFAULT_ROULETTE_SAFE_REWARD_STEP,
            minimum=0, maximum=1_000_000, warnings=warnings,
            label="roulette.safe_reward_step",
        ),
        hit_penalty=_int(
            roulette_raw, "hit_penalty", DEFAULT_ROULETTE_HIT_PENALTY,
            minimum=0, maximum=10_000_000, warnings=warnings,
            label="roulette.hit_penalty",
        ),
        share_percent=_int(
            roulette_raw, "share_percent", DEFAULT_ROULETTE_SHARE_PERCENT,
            minimum=0, maximum=100, warnings=warnings, label="roulette.share_percent",
        ),
        misfire_percent=_float(
            roulette_raw, "misfire_percent", DEFAULT_ROULETTE_MISFIRE_PERCENT,
            minimum=0, maximum=100, warnings=warnings,
            label="roulette.misfire_percent",
        ),
        misfire_reward=_int(
            roulette_raw, "misfire_reward", DEFAULT_ROULETTE_MISFIRE_REWARD,
            minimum=0, maximum=1_000_000, warnings=warnings,
            label="roulette.misfire_reward",
        ),
        drunk_extra_max=_int(
            roulette_raw, "drunk_extra_max", DEFAULT_ROULETTE_DRUNK_EXTRA_MAX,
            minimum=0, maximum=20, warnings=warnings,
            label="roulette.drunk_extra_max",
        ),
        drunk_extra_percent=_float(
            roulette_raw, "drunk_extra_percent", DEFAULT_ROULETTE_DRUNK_EXTRA_PERCENT,
            minimum=0, maximum=100, warnings=warnings,
            label="roulette.drunk_extra_percent",
        ),
        mute_seconds=_int(
            roulette_raw, "mute_seconds", DEFAULT_ROULETTE_MUTE_SECONDS,
            minimum=0, maximum=2_592_000, warnings=warnings,
            label="roulette.mute_seconds",
        ),
        timeout_seconds=_int(
            roulette_raw, "timeout_seconds", DEFAULT_ROULETTE_TIMEOUT_SECONDS,
            minimum=30, maximum=86_400, warnings=warnings,
            label="roulette.timeout_seconds",
        ),
        rescue_fail_percent=_float(
            roulette_raw, "rescue_fail_percent", DEFAULT_ROULETTE_RESCUE_FAIL_PERCENT,
            minimum=0, maximum=100, warnings=warnings,
            label="roulette.rescue_fail_percent",
        ),
        judgment_seconds=_int(
            roulette_raw, "judgment_seconds", DEFAULT_ROULETTE_JUDGMENT_SECONDS,
            minimum=0, maximum=2_592_000, warnings=warnings,
            label="roulette.judgment_seconds",
        ),
        judgment_fail_percent=_float(
            roulette_raw, "judgment_fail_percent", DEFAULT_ROULETTE_JUDGMENT_FAIL_PERCENT,
            minimum=0, maximum=100, warnings=warnings,
            label="roulette.judgment_fail_percent",
        ),
        judgment_backfire_percent=_float(
            roulette_raw, "judgment_backfire_percent",
            DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_PERCENT,
            minimum=0, maximum=100, warnings=warnings,
            label="roulette.judgment_backfire_percent",
        ),
        judgment_backfire_drunk_only=_bool(
            roulette_raw, "judgment_backfire_drunk_only",
            DEFAULT_ROULETTE_JUDGMENT_BACKFIRE_DRUNK_ONLY,
            warnings=warnings, label="roulette.judgment_backfire_drunk_only",
        ),
        action_cooldown_seconds=_int(
            roulette_raw, "action_cooldown_seconds",
            DEFAULT_ROULETTE_ACTION_COOLDOWN_SECONDS,
            minimum=0, maximum=86_400, warnings=warnings,
            label="roulette.action_cooldown_seconds",
        ),
    )

    return Settings(
        access=access,
        checkin=checkin,
        coins=coins,
        levels=levels,
        import_=import_settings,
        ui=ui,
        duel=duel,
        roulette=roulette,
        warnings=tuple(warnings),
    )
