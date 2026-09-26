"""决斗：掷点（平局加赛）、赔付倍率、奖惩规则、免罚、禁言与流水。

用假的平台实现替换真实禁言调用。赔付倍率另有一组纯函数单测，见「赔付倍率」一节。
"""

from __future__ import annotations

import random
from datetime import timedelta, timezone

import pytest

from funhub.config import DuelSettings, load_settings
from funhub.duel import (
    MODE_COIN,
    MODE_COIN_AND_MUTE,
    MODE_IMMUNE,
    SIDE_CHALLENGER,
    SIDE_TARGET,
    DuelService,
    plan_penalty,
    roll_duel,
    roll_once,
)
from funhub.platform_api import (
    REASON_ERROR,
    REASON_PROTECTED,
    REASON_UNSUPPORTED,
    GroupPlatform,
    MuteOutcome,
)
from funhub.players import PlayerRepository

from .conftest import identity

HK = timezone(timedelta(hours=8))

#: 掷点预设：挑战者 10 : 对手 90 → 挑事的输；反过来则是被打的输。
CHALLENGER_LOSES = (10, 90)
TARGET_LOSES = (90, 10)


class StubRng:
    """让掷点与基础赔付额可控：按顺序吐出预设值。"""

    def __init__(self, *values: int) -> None:
        self.values = list(values)
        self.last = 0

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0) if self.values else self.last
        self.last = value
        return min(max(value, low), high)


class StubPlatform(GroupPlatform):
    """可控的群平台。``roles`` 是 user_id → 群内角色，未列出的按普通成员算。"""

    def __init__(
        self,
        *,
        role: str | None = None,
        muted: bool | None = False,
        mute_ok: bool = True,
        mute_reason: str = "",
        roles: dict[str, str] | None = None,
    ) -> None:
        self._role = role
        self._muted = muted
        self._mute_ok = mute_ok
        self._mute_reason = mute_reason
        self._roles = dict(roles or {})
        self.mute_calls: list[tuple[str, int]] = []

    async def bot_role(self) -> str | None:
        return self._role

    async def member_role(self, user_id: str) -> str | None:
        return self._roles.get(user_id, "member")

    async def is_muted(self, user_id: str) -> bool | None:
        del user_id
        return self._muted

    async def mute(self, user_id: str, seconds: int) -> MuteOutcome:
        self.mute_calls.append((user_id, seconds))
        if self._mute_ok:
            return MuteOutcome(True)
        return MuteOutcome(False, self._mute_reason or REASON_UNSUPPORTED)


async def seed_player(
    database,
    user_id: str,
    *,
    coins: int,
    group_id: str = "100",
    nickname: str | None = None,
) -> int:
    """建号并给币（走流水），返回 player_pk。"""
    repository = PlayerRepository(database)
    player_identity = identity(
        user_id=user_id,
        nickname=user_id if nickname is None else nickname,
        group_id=group_id,
    )
    async with database.transaction() as connection:
        player, _ = await repository.get_or_create_in_db(connection, player_identity)
        if coins:
            await repository.apply_coin_delta_in_db(
                connection,
                player_pk=player.id,
                delta=coins,
                reason="seed",
            )
    return player.id


async def seed_both(database, *, coins: int = 2000) -> None:
    """双方等额建号 —— 存款一样，倍率保持 1.0，方便断言基础规则。"""
    await seed_player(database, "a", coins=coins)
    await seed_player(database, "b", coins=coins)


def make_service(database, *, rng_values=(50, 10, 200), settings=None) -> DuelService:
    settings = settings or load_settings({})
    return DuelService(database, settings, rng=StubRng(*rng_values))


async def run_duel(database, service, platform, *, challenger="a", target="b"):
    return await service.duel(
        identity(user_id=challenger, nickname="龟甲"),
        identity(user_id=target, nickname="龟乙"),
        platform=platform,
        zone=HK,
    )


# ---------------------------------------------------------------------------
# 掷点：没有平局
# ---------------------------------------------------------------------------


def test_roll_once_is_a_single_round():
    round_ = roll_once(StubRng(70, 30), dice_max=100)
    assert (round_.challenger, round_.target) == (70, 30)
    assert round_.tied is False


def test_roll_decides_with_one_round_when_points_differ():
    roll = roll_duel(StubRng(70, 30), dice_max=100)
    assert roll.rounds == (roll.final,)
    assert roll.overtime is False
    assert roll.winner == SIDE_CHALLENGER
    assert roll_duel(StubRng(30, 70), dice_max=100).winner == SIDE_TARGET


def test_tie_goes_to_overtime_until_someone_wins():
    roll = roll_duel(StubRng(50, 50, 80, 20), dice_max=100)
    assert roll.overtime is True
    assert [(item.challenger, item.target) for item in roll.rounds] == [(50, 50), (80, 20)]
    assert roll.winner == SIDE_CHALLENGER


def test_overtime_can_be_won_by_the_target():
    roll = roll_duel(StubRng(7, 7, 7, 7, 3, 9), dice_max=100)
    assert len(roll.rounds) == 3
    assert roll.winner == SIDE_TARGET


def test_degenerate_dice_still_terminates():
    """骰子退化成常数（或注入恒定随机源）时不会死循环，判挑战者胜。"""

    class Constant:
        def randint(self, low: int, high: int) -> int:
            return low

    roll = roll_duel(Constant(), dice_max=100)
    assert len(roll.rounds) == 10
    assert roll.forced is True
    assert roll.winner == SIDE_CHALLENGER


def test_dice_is_bounded_by_config():
    settings = load_settings({"duel": {"dice_max": 10}})
    rng = random.Random(7)
    for _ in range(200):
        roll = roll_duel(rng, dice_max=settings.duel.dice_max)
        for item in roll.rounds:
            assert 0 <= item.challenger <= 10
            assert 0 <= item.target <= 10
        assert roll.final.tied is False  # 真实随机源下总能分出胜负


def test_small_dice_max_resolves_ties_quickly():
    """骰子只有 0/1 时平局很常见，加赛也要能收敛。"""
    rng = random.Random(3)
    for _ in range(200):
        roll = roll_duel(rng, dice_max=1)
        assert roll.final.tied is False
        assert len(roll.rounds) <= 20


# ---------------------------------------------------------------------------
# 赔付倍率：存款碾压 + 管理员败北
# ---------------------------------------------------------------------------

DUEL = load_settings({}).duel


def penalty(
    *,
    base: int = 500,
    loser: int = 0,
    winner: int = 0,
    admin: bool = False,
    settings: DuelSettings | None = None,
):
    return plan_penalty(
        settings or DUEL,
        base=base,
        loser_coins=loser,
        winner_coins=winner,
        loser_is_admin=admin,
    )


def test_defaults_match_the_documented_numbers():
    assert DUEL.stake_max == 500
    assert DUEL.rich_step_coins == 500
    assert DUEL.rich_step_percent == 10
    assert DUEL.rich_cap_percent == 50
    assert DUEL.admin_percent == 20  # 管理员在普通人倍率上再加 0.2


def test_no_bonus_when_the_loser_is_not_richer():
    plan = penalty(base=500, loser=1000, winner=1000)
    assert plan.rich_percent == 0
    assert plan.multiplier == 1.0
    assert plan.total == 500
    assert plan.boosted is False

    poorer = penalty(base=500, loser=100, winner=900)
    assert poorer.rich_excess == 0  # 比胜者穷不算，也不会倒扣
    assert poorer.total == 500


@pytest.mark.parametrize(
    ("excess", "percent", "total"),
    [
        (0, 0, 500),
        (499, 0, 500),
        (500, 10, 550),
        (1000, 20, 600),
        (1500, 30, 650),
        (2000, 40, 700),
        (2500, 50, 750),
        (3000, 50, 750),  # 封顶
        (99999, 50, 750),
    ],
)
def test_rich_penalty_grows_every_step_and_caps(excess: int, percent: int, total: int):
    plan = penalty(base=500, loser=excess, winner=0)
    assert plan.rich_percent == percent
    assert plan.total == total


@pytest.mark.parametrize(
    ("excess", "percent", "multiplier", "total"),
    [
        (0, 0, 1.2, 600),  # 普通人 1.0 + 管理员 0.2
        (1000, 20, 1.4, 700),  # 与存款碾压加算
        (2500, 50, 1.7, 850),  # 存款碾压封顶 + 管理员 0.2
        (99999, 50, 1.7, 850),
    ],
)
def test_admin_bonus_stacks_with_the_rich_penalty(
    excess: int,
    percent: int,
    multiplier: float,
    total: int,
):
    plan = penalty(base=500, loser=excess, winner=0, admin=True)
    assert plan.rich_percent == percent
    assert plan.admin_percent == 20
    assert plan.multiplier == multiplier
    assert plan.total == total


def test_admin_bonus_is_additive_not_a_fixed_multiplier():
    """管理员的 0.2 是加在普通人倍率上的加数，普通人倍率变了它也跟着变。"""
    plain = penalty(base=500, loser=1500, winner=0)
    admin = penalty(base=500, loser=1500, winner=0, admin=True)
    assert plain.multiplier == 1.3
    assert admin.multiplier == 1.5  # 1.3 + 0.2，不是固定的 1.2 或 1.5 封顶


def test_loser_side_max_is_1_5x_and_admin_max_is_1_7x():
    """默认配置下的上限：普通人 1.5 倍，管理员 1.5 + 0.2 = 1.7 倍。"""
    assert penalty(base=500, loser=10**9, winner=0).multiplier == 1.5
    assert penalty(base=500, loser=10**9, winner=0, admin=True).multiplier == 1.7


def test_total_is_rounded_half_up():
    assert penalty(base=505, loser=500, winner=0).total == 556  # 505 × 1.1 = 555.5
    assert penalty(base=0, loser=500, winner=0, admin=True).total == 0


def test_multiplier_is_configurable():
    settings = load_settings(
        {
            "duel": {
                "rich_step_coins": 100,
                "rich_step_percent": 25,
                "rich_cap_percent": 100,
                "admin_percent": 10,
            },
        },
    ).duel
    plan = penalty(base=200, loser=300, winner=0, admin=True, settings=settings)
    assert plan.rich_percent == 75  # 300 // 100 × 25
    assert plan.admin_percent == 10
    assert plan.total == 370  # 200 × 1.85


# ---------------------------------------------------------------------------
# 三种结局
# ---------------------------------------------------------------------------


async def test_coin_only_when_bot_is_not_admin(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    platform = StubPlatform(role="member")

    result = await run_duel(database, service, platform)

    assert result.mode == MODE_COIN
    assert result.winner.user_id == "a"
    assert result.loser.user_id == "b"
    assert result.stake == 200
    assert result.transferred == 200
    assert platform.mute_calls == []  # 不是管理员，压根不尝试禁言
    assert result.loser.coins == 1800
    assert result.winner.coins == 2200


async def test_admin_bot_mutes_the_losing_challenger(database):
    """挑事的输了 → 禁言 + 赔付。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role="admin")

    result = await run_duel(database, service, platform)

    assert result.mode == MODE_COIN_AND_MUTE
    assert result.muted is True
    assert result.mute_seconds == 60
    assert result.transferred == 200
    assert platform.mute_calls == [("a", 60)]
    assert await service.players.muted_until(result.loser.id) is not None


async def test_losing_target_is_never_muted(database):
    """被打的人输了只赔币，不会被禁言 —— 机器人是管理员也一样。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    platform = StubPlatform(role="admin")

    result = await run_duel(database, service, platform)

    assert result.loser.user_id == "b"
    assert result.mode == MODE_COIN
    assert result.muted is False
    assert platform.mute_calls == []
    assert await service.players.muted_until(result.loser.id) is None


async def test_admin_loser_pays_a_bit_again(database):
    """败者是管理员：平台根本禁不了他，惩罚折成在普通人倍率上再加 0.2 倍。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    platform = StubPlatform(role="admin", roles={"b": "owner"})

    result = await run_duel(database, service, platform)

    assert platform.mute_calls == []  # 明知禁不了就不白调接口
    assert result.mode == MODE_COIN
    assert result.penalty.admin_percent == 20
    assert result.stake == 240  # 200 × 1.2
    assert result.transferred == 240
    assert result.loser.coins == 1760
    assert result.winner.coins == 2240


async def test_admin_challenger_losing_also_pays_a_bit_again(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role="admin", roles={"a": "admin"})

    result = await run_duel(database, service, platform)

    assert platform.mute_calls == []
    assert result.penalty.admin_percent == 20
    assert result.transferred == 240


async def test_rich_penalty_applies_in_a_real_duel(database):
    """存款碾压：挑事者比对手多 2000 → 多赔 40%（禁言照旧）。"""
    await seed_player(database, "a", coins=2500)
    await seed_player(database, "b", coins=500)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role="admin", roles={"a": "member"})

    result = await run_duel(database, service, platform)

    assert result.penalty.rich_excess == 2000
    assert result.penalty.rich_percent == 40
    assert result.stake == 280  # 200 × 1.4
    assert result.transferred == 280
    assert result.muted is True  # 普通成员照样禁言


async def test_generic_mute_failure_does_not_boost_the_penalty(database):
    """禁言失败但不是"对方不能被禁言"，不额外多赔。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(
        role="admin",
        mute_ok=False,
        mute_reason=REASON_ERROR,
        roles={"a": "member"},
    )

    result = await run_duel(database, service, platform)

    assert result.muted is False
    assert result.mute_failure == REASON_ERROR
    assert result.penalty.admin_percent == 0
    assert result.transferred == 200


async def test_unknown_role_uses_the_error_classification(database):
    """QQ 官方机器人查不到成员角色，只能靠"对方受保护"的错误归类补上管理员加成。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(
        role=None,
        mute_ok=False,
        mute_reason=REASON_PROTECTED,
        roles={},  # member_role 一律返回 member → 查不到
    )
    platform._roles = {}  # noqa: SLF001 - 明确"查不到角色"

    async def unknown_role(user_id: str) -> str | None:
        del user_id
        return None

    platform.member_role = unknown_role  # type: ignore[method-assign]

    result = await run_duel(database, service, platform)

    assert platform.mute_calls == [("a", 60)]
    assert result.penalty.admin_percent == 20
    assert result.transferred == 240


async def test_unknown_role_with_a_plain_failure_pays_no_bonus(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role=None, mute_ok=False, mute_reason=REASON_UNSUPPORTED)

    async def unknown_role(user_id: str) -> str | None:
        del user_id
        return None

    platform.member_role = unknown_role  # type: ignore[method-assign]

    result = await run_duel(database, service, platform)

    assert result.penalty.admin_percent == 0
    assert result.transferred == 200


async def test_winner_being_an_admin_does_not_earn_extra(database):
    """倍率只看败者身份，胜者是不是管理员不影响。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(
        role="admin",
        mute_ok=True,
        roles={"a": "member", "b": "admin"},
    )

    result = await run_duel(database, service, platform)

    assert result.penalty.admin_percent == 0
    assert result.muted is True
    assert result.transferred == 200


async def test_admin_role_owner_also_mutes(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role="owner")
    result = await run_duel(database, service, platform)
    assert result.mode == MODE_COIN_AND_MUTE


# ---------------------------------------------------------------------------
# 赔付"尽所能"：钱包封顶，且不设豁免门槛
# ---------------------------------------------------------------------------


async def test_broke_challenger_pays_everything_they_have(database):
    """余额不够就全给，穷也得受罚（禁言照旧）。"""
    await seed_player(database, "a", coins=10)
    await seed_player(database, "b", coins=10)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role="admin")

    result = await run_duel(database, service, platform)

    assert result.mode == MODE_COIN_AND_MUTE
    assert result.stake == 200  # 应赔
    assert result.transferred == 10  # 实付
    assert result.loser.coins == 0
    assert result.winner.coins == 20
    assert platform.mute_calls == [("a", 60)]


async def test_penniless_loser_still_loses_the_duel(database):
    await seed_both(database, coins=0)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    platform = StubPlatform(role="member")

    result = await run_duel(database, service, platform)

    assert result.mode == MODE_COIN
    assert result.transferred == 0
    assert result.winner.coins == 0
    assert result.roll.final.challenger == 90  # 点数照掷、胜负照判


async def test_admin_cap_is_affordable_for_a_rich_admin(database):
    """管理员 + 存款碾压 → 1.5 + 0.2 = 1.7 倍；钱够就全额赔。"""
    await seed_player(database, "a", coins=0)
    await seed_player(database, "b", coins=3000)
    service = make_service(database, rng_values=(*TARGET_LOSES, 500))
    platform = StubPlatform(role="admin", roles={"b": "owner"})

    result = await run_duel(database, service, platform)

    assert result.penalty.multiplier == 1.7
    assert result.stake == 850
    assert result.transferred == 850
    assert result.loser.coins == 2150


# ---------------------------------------------------------------------------
# 免罚：只有"该被禁言却已经在禁言里"的挑事者
# ---------------------------------------------------------------------------


async def test_platform_muted_challenger_is_immune(database):
    await seed_both(database, coins=5000)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="admin", muted=True))
    assert result.mode == MODE_IMMUNE
    assert result.immune_reason == "already_muted"
    assert result.transferred == 0
    assert result.loser.user_id == "a"


async def test_muted_target_still_pays(database):
    """被打的人即使正在禁言中，也照常赔币 —— 他本来就不会被禁言。"""
    await seed_both(database, coins=5000)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="admin", muted=True))

    assert result.mode == MODE_COIN
    assert result.immune_reason == ""
    assert result.transferred == 200
    assert result.loser.coins == 4800


async def test_our_own_mute_record_makes_the_challenger_immune(database):
    """官方机器人查不到成员禁言状态，靠我们自己的记录兜底。"""
    await seed_both(database, coins=5000)
    first = await run_duel(
        database,
        make_service(database, rng_values=(*CHALLENGER_LOSES, 200)),
        StubPlatform(role="admin"),
    )
    assert first.muted is True

    second = await run_duel(
        database,
        make_service(database, rng_values=(*CHALLENGER_LOSES, 200)),
        StubPlatform(role="admin", muted=None),
    )
    assert second.mode == MODE_IMMUNE
    assert second.immune_reason == "already_muted"


async def test_immune_still_counts_as_a_duel(database):
    """免罚只免惩罚，决斗本身照常进行（点数照掷）。"""
    await seed_both(database, coins=5000)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="admin", muted=True))
    assert result.roll.final.challenger == 10
    assert result.roll.final.target == 90
    assert result.winner.user_id == "b"
    assert result.loser.user_id == "a"


# ---------------------------------------------------------------------------
# 规则真值表：禁言只落在"主动挑事且输掉"的一方；倍率看败者身份
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dice", "bot_role", "loser_is_admin", "mute_ok", "expect_muted", "expect_admin_percent"),
    [
        # 挑事的人输了
        (CHALLENGER_LOSES, "admin", False, True, True, 0),
        (CHALLENGER_LOSES, "admin", False, False, False, 0),
        (CHALLENGER_LOSES, "admin", True, False, False, 20),
        (CHALLENGER_LOSES, "member", False, True, False, 0),
        (CHALLENGER_LOSES, "member", True, True, False, 20),
        (CHALLENGER_LOSES, None, True, False, False, 20),
        (CHALLENGER_LOSES, None, False, False, False, 0),
        # 被打的人输了：一律不禁言，但管理员依旧多赔
        (TARGET_LOSES, "admin", True, True, False, 20),
        (TARGET_LOSES, "admin", False, True, False, 0),
        (TARGET_LOSES, "owner", True, False, False, 20),
        (TARGET_LOSES, "member", True, True, False, 20),
        (TARGET_LOSES, None, True, False, False, 20),
    ],
)
async def test_mute_rules_truth_table(
    database,
    dice: tuple[int, int],
    bot_role: str | None,
    loser_is_admin: bool,
    mute_ok: bool,
    expect_muted: bool,
    expect_admin_percent: int,
):
    """禁言只跟"谁先挑事"有关；管理员加成只跟"败者是谁"有关。"""
    challenger_role = "owner" if (loser_is_admin and dice == CHALLENGER_LOSES) else "member"
    target_role = "owner" if (loser_is_admin and dice == TARGET_LOSES) else "member"
    await seed_both(database)

    service = make_service(database, rng_values=(*dice, 200))
    platform = StubPlatform(
        role=bot_role,
        mute_ok=mute_ok,
        # 平台报错的口径要和"败者能不能被禁言"一致：管理员 → 对方受保护
        mute_reason=REASON_PROTECTED if loser_is_admin else REASON_ERROR,
        roles={"a": challenger_role, "b": target_role},
    )

    result = await run_duel(database, service, platform)

    assert result.muted is expect_muted
    assert result.penalty.admin_percent == expect_admin_percent
    assert result.penalty.rich_percent == 0  # 双方等额
    assert result.stake == 200 + expect_admin_percent * 2  # 200 × (1 + 20%)
    loser_id = "a" if dice == CHALLENGER_LOSES else "b"
    assert result.loser.user_id == loser_id
    if expect_muted:
        assert platform.mute_calls == [(loser_id, 60)]
    elif dice == TARGET_LOSES or loser_is_admin or bot_role == "member":
        assert platform.mute_calls == []


# ---------------------------------------------------------------------------
# 流水、无冷却、分组
# ---------------------------------------------------------------------------


async def test_challenger_can_lose_and_be_punished(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 300))
    platform = StubPlatform(role="admin")

    result = await run_duel(database, service, platform)

    assert result.roll.winner == SIDE_TARGET
    assert result.loser.user_id == "a"
    assert result.winner.user_id == "b"
    assert result.transferred == 300
    assert platform.mute_calls == [("a", 60)]


async def test_ledger_keeps_balances_and_totals_consistent(database):
    await seed_both(database, coins=3000)
    service = make_service(database, rng_values=(*TARGET_LOSES, 400))

    # 败者(b) 是群主 → 400 × (1.0 + 0.2) = 480
    result = await run_duel(
        database,
        service,
        StubPlatform(role="admin", roles={"b": "owner"}),
    )
    assert result.transferred == 480

    for user_id in ("a", "b"):
        row = await database.fetchone("SELECT id FROM players WHERE user_id = ?", (user_id,))
        coins, total, ledger = await service.players.coins_invariant_in_db(int(row["id"]))
        assert ledger == coins, user_id
        assert total >= coins, user_id  # 累计获得只增不减

    reasons = {
        row["reason"]: int(row["total"])
        for row in await database.fetchall(
            "SELECT reason, COUNT(*) AS total FROM coin_ledger GROUP BY reason",
        )
    }
    assert reasons["duel_lose"] == 1
    assert reasons["duel_win"] == 1


async def test_total_coins_only_grows_for_the_loser(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="member"))

    assert result.loser.coins == 1800
    assert result.loser.total_coins == 2000  # 累计获得不因输钱变小
    assert result.winner.coins == 2200
    assert result.winner.total_coins == 2200


async def test_duel_has_no_cooldown(database):
    """连续挑事不受冷却限制（每次都真的结算）。"""
    await seed_both(database, coins=1000)
    service = make_service(
        database,
        rng_values=(
            *CHALLENGER_LOSES, 100,
            *CHALLENGER_LOSES, 100,
            *CHALLENGER_LOSES, 100,
            *CHALLENGER_LOSES, 100,
        ),
    )
    platform = StubPlatform(role="member")
    for _ in range(4):
        result = await run_duel(database, service, platform)
        assert result.mode == MODE_COIN
        assert result.transferred == 100  # 挑战者越输越穷，碾压加成始终为 0
    row = await database.fetchone("SELECT coins FROM players WHERE user_id = 'a'")
    assert int(row["coins"]) == 600
    winner = await database.fetchone("SELECT coins FROM players WHERE user_id = 'b'")
    assert int(winner["coins"]) == 1400


async def test_duel_is_scoped_per_group(database):
    """同一个人在另一个群是另一份数据。"""
    await seed_player(database, "a", coins=2000)  # 群 100
    await seed_player(database, "a", coins=5000, group_id="200")
    await seed_player(database, "b", coins=2000)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 300))

    result = await run_duel(database, service, StubPlatform(role="member"))

    assert result.loser.coins == 1700  # 群 100 里的 a 挨罚
    other = await database.fetchone(
        "SELECT coins FROM players WHERE user_id = 'a' AND group_id = '200'",
    )
    assert int(other["coins"]) == 5000  # 群 200 的 a 不受影响


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


async def test_render_duel_card(database):
    """挑事的输了：卡片带禁言那一行。"""
    await seed_both(database)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="admin"))

    assert service.render(result).splitlines() == [
        "决斗 · 龟甲 vs 龟乙",
        "10 : 90",
        "龟乙 胜，赢得 200 龟龟币",
        "龟甲 被禁言 1 分钟",
    ]


async def test_render_target_loss_card_has_no_mute_line(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="admin"))
    assert service.render(result).splitlines()[2:] == ["龟甲 胜，赢得 200 龟龟币"]


async def test_render_rich_penalty_note(database):
    await seed_player(database, "a", coins=100)
    await seed_player(database, "b", coins=1100)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="member"))

    assert service.render(result).splitlines()[-1] == "龟乙 存款多 1000，赔付 ×1.2"


async def test_render_admin_penalty_note(database):
    await seed_both(database)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    result = await run_duel(
        database,
        service,
        StubPlatform(role="member", roles={"b": "admin"}),
    )

    assert service.render(result).splitlines()[-1] == "龟乙 是管理员，赔付 ×1.2"


async def test_render_immune_card(database):
    await seed_both(database, coins=5000)
    service = make_service(database, rng_values=(*CHALLENGER_LOSES, 200))
    immune = await run_duel(database, service, StubPlatform(role="admin", muted=True))
    assert immune.mode == MODE_IMMUNE
    assert service.render(immune).endswith("龟乙 胜，龟甲 正在禁言中，本次不罚")


async def test_render_card_for_a_broke_loser(database):
    await seed_player(database, "a", coins=10)
    await seed_player(database, "b", coins=10)
    service = make_service(database, rng_values=(*TARGET_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="member"))
    assert service.render(result).splitlines()[2] == "龟甲 胜，赢得 10 龟龟币"


async def test_render_overtime_card(database):
    """加赛的点数排在一行里，一眼能看出加赛过。"""
    await seed_both(database)
    service = make_service(database, rng_values=(50, 50, *TARGET_LOSES, 200))
    result = await run_duel(database, service, StubPlatform(role="member"))

    lines = service.render(result).splitlines()
    assert lines[1] == "50 : 50 → 90 : 10"
    assert lines[2] == "龟甲 胜，赢得 200 龟龟币"


async def test_render_compacts_a_very_long_overtime(database):
    class Constant:
        def randint(self, low: int, high: int) -> int:
            return high

    await seed_both(database)
    service = DuelService(database, load_settings({}), rng=Constant())
    result = await run_duel(database, service, StubPlatform(role="member"))
    assert result.roll.forced is True
    assert service.render(result).splitlines()[1] == "100 : 100 → … → 100 : 100"


async def test_mute_seconds_are_configurable(database):
    await seed_both(database)
    settings = load_settings({"duel": {"mute_seconds": 120}})
    service = DuelService(database, settings, rng=StubRng(*CHALLENGER_LOSES, 200))
    platform = StubPlatform(role="admin")

    result = await run_duel(database, service, platform)

    assert platform.mute_calls == [("a", 120)]
    assert "被禁言 2 分钟" in service.render(result)
