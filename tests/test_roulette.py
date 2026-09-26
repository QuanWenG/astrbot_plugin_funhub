"""龟龟轮盘：弹槽预置、空枪递增、子弹那一枪炸膛、中弹扣币平分、禁言保护、超时与救援。

玩法对照 Pallas-Bot `packages/roulette`：子弹位置开局定好，所以连开就是赌命。
"""

from __future__ import annotations

import pytest

from funhub.config import load_settings
from funhub.drunk import DrunkState
from funhub.roulette import (
    REASON_MISFIRE,
    REASON_HIT,
    REASON_SAFE,
    REASON_SHARE,
    SHOT_MISFIRE,
    SHOT_HIT,
    SHOT_MISS,
    SHOT_NO_ROUND,
    RescueOutcome,
    RouletteService,
)
from funhub.platform_api import REASON_ERROR, GroupPlatform, MuteOutcome
from funhub.players import PlayerRepository

from .conftest import FakeAt, identity


class StubRng:
    """按顺序吐出预设值；用完就用默认值。

    ``randint`` 默认 1（弹槽），``random`` 默认 0.99（子弹那一枪不炸膛、救援不失败）。
    """

    def __init__(
        self,
        *values: int | float,
        default_int: int = 1,
        default_random: float = 0.99,
    ) -> None:
        self.values = list(values)
        self.default_int = default_int
        self.default_random = default_random

    def _next(self) -> int | float:
        return self.values.pop(0) if self.values else self.default_int

    def randint(self, low: int, high: int) -> int:
        value = self._next() if self.values else self.default_int
        return min(max(int(value), low), high)

    def random(self) -> float:
        return float(self.values.pop(0)) if self.values else self.default_random


class StubClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubPlatform(GroupPlatform):
    def __init__(
        self,
        *,
        mute_ok: bool = True,
        mute_reason: str = "",
        roles: dict[str, str] | None = None,
    ) -> None:
        self._mute_ok = mute_ok
        self._mute_reason = mute_reason
        self._roles = dict(roles or {})
        self.mute_calls: list[tuple[str, int]] = []

    async def member_role(self, user_id: str) -> str | None:
        return self._roles.get(user_id, "member")

    async def mute(self, user_id: str, seconds: int) -> MuteOutcome:
        self.mute_calls.append((user_id, seconds))
        if self._mute_ok:
            return MuteOutcome(True)
        return MuteOutcome(False, self._mute_reason or REASON_ERROR)


def make_service(
    database,
    *,
    chamber: int = 3,
    values: tuple = (),
    clock: StubClock | None = None,
    settings=None,
) -> RouletteService:
    """把子弹放在第 3 个弹槽；随机源默认 0.99（子弹那一枪不炸膛）。"""
    return RouletteService(
        database,
        settings or load_settings({}),
        rng=StubRng(chamber, *values),
        clock=clock or StubClock(),
    )


async def seed_coins(database, user_id: str, coins: int) -> None:
    repository = PlayerRepository(database)
    async with database.transaction() as connection:
        player, _ = await repository.get_or_create_in_db(
            connection,
            identity(user_id=user_id, nickname=user_id),
        )
        if coins:
            await repository.apply_coin_delta_in_db(
                connection,
                player_pk=player.id,
                delta=coins,
                reason="seed",
            )


async def coins_of(database, user_id: str) -> int:
    row = await database.fetchone(
        "SELECT coins FROM players WHERE user_id = ?",
        (user_id,),
    )
    return int(row["coins"]) if row else 0


# ---------------------------------------------------------------------------
# 开局
# ---------------------------------------------------------------------------


async def test_start_creates_a_round_with_a_hidden_chamber(database):
    service = make_service(database, chamber=3)
    outcome = await service.start(identity(user_id="a"))

    assert outcome.started is True
    assert outcome.busy is False
    assert outcome.chambers == 6
    assert outcome.safe_reward == 50
    assert outcome.safe_reward_step == 50
    assert outcome.hit_penalty == 500
    assert outcome.misfire_percent == 25
    assert outcome.misfire_reward == 500
    assert outcome.mute_seconds == 60
    room = service._rounds[service.key_for(identity(user_id="a"))]  # noqa: SLF001
    assert room.chamber == 3
    assert room.shooters == []  # 发起者没开枪就不算开过枪


async def test_only_one_round_per_group(database):
    service = make_service(database, chamber=3)
    await service.start(identity(user_id="a"))
    again = await service.start(identity(user_id="b"))

    assert again.started is False
    assert again.busy is True


async def test_other_groups_are_independent(database):
    service = make_service(database, chamber=3)
    await service.start(identity(user_id="a", group_id="100"))
    other = await service.start(identity(user_id="a", group_id="200"))
    assert other.started is True


# ---------------------------------------------------------------------------
# 开枪
# ---------------------------------------------------------------------------


async def test_shooting_without_a_round(database):
    service = make_service(database, chamber=3)
    outcome = await service.shoot(identity(user_id="a"), platform=StubPlatform())
    assert outcome.kind == SHOT_NO_ROUND
    assert await coins_of(database, "a") == 0


async def test_safe_shots_pay_the_shooter(database):
    service = make_service(database, chamber=3)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    first = await service.shoot(identity(user_id="b"), platform=platform)
    second = await service.shoot(identity(user_id="c"), platform=platform)

    assert (first.kind, first.shots, first.reward) == (SHOT_MISS, 1, 50)
    assert (second.kind, second.shots, second.reward) == (SHOT_MISS, 2, 100)
    assert await coins_of(database, "b") == 50
    assert await coins_of(database, "c") == 100
    assert await coins_of(database, "a") == 0  # 只发起、没开枪就没有奖


async def test_safe_reward_grows_every_shot(database):
    """第一枪 50，之后每枪多 50：50 / 100 / 150 / 200 / 250。"""
    service = make_service(database, chamber=6)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    rewards = []
    for index in range(5):
        outcome = await service.shoot(identity(user_id=f"p{index}"), platform=platform)
        assert outcome.kind == SHOT_MISS
        rewards.append(outcome.reward)

    assert rewards == [50, 100, 150, 200, 250]
    assert await coins_of(database, "p4") == 250


async def test_same_person_can_keep_shooting(database):
    service = make_service(database, chamber=4)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    first = await service.shoot(identity(user_id="b"), platform=platform)
    second = await service.shoot(identity(user_id="b"), platform=platform)

    assert (first.kind, second.kind) == (SHOT_MISS, SHOT_MISS)
    assert (first.reward, second.reward) == (50, 100)
    assert await coins_of(database, "b") == 150  # 同一人连开也按枪数递增


async def test_the_chamber_shot_hits(database):
    await seed_coins(database, "b", 1000)
    service = make_service(database, chamber=3)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    await service.shoot(identity(user_id="a"), platform=platform)
    await service.shoot(identity(user_id="c"), platform=platform)
    hit = await service.shoot(identity(user_id="b"), platform=platform)

    assert hit.kind == SHOT_HIT
    assert hit.shots == 3
    assert hit.penalty == 500
    assert hit.paid == 500
    assert hit.muted is True
    assert hit.mute_seconds == 60
    assert await coins_of(database, "b") == 500
    assert platform.mute_calls == [("b", 60)]


async def test_hit_penalty_paid_and_split(database):
    await seed_coins(database, "c", 1000)
    service = make_service(database, chamber=3)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="a", nickname="甲"), platform=platform)  # a +50
    await service.shoot(identity(user_id="b", nickname="乙"), platform=platform)  # b +100

    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.paid == 500
    assert hit.shared == 500  # 罚多少就分多少
    assert hit.sharers == 2
    assert hit.share_each == 250
    assert hit.share_names == ("甲", "乙")  # 卡片直接写名字
    assert await coins_of(database, "c") == 500
    assert await coins_of(database, "a") == 50 + 250
    assert await coins_of(database, "b") == 100 + 250


async def test_non_shooter_gets_no_share(database):
    """只发起、没开枪的人不参与分账。"""
    await seed_coins(database, "c", 1000)
    service = make_service(database, chamber=2)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="b", nickname="乙"), platform=platform)  # b +50

    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.shared == 500 and hit.sharers == 1
    assert hit.share_names == ("乙",)
    assert await coins_of(database, "a") == 0
    assert await coins_of(database, "b") == 50 + 500


async def test_share_names_use_the_group_card(database):
    """分账名单写群名片，不写 user_id。"""
    await seed_coins(database, "c", 1000)
    service = make_service(database, chamber=3)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    await service.shoot(identity(user_id="a", nickname="龟甲"), platform=platform)
    await service.shoot(identity(user_id="b", nickname="龟乙"), platform=platform)
    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.share_names == ("龟甲", "龟乙")


async def test_poor_victim_pays_what_they_have(database):
    await seed_coins(database, "c", 120)
    service = make_service(database, chamber=2)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="a"), platform=platform)  # a +50

    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.paid == 120
    assert hit.coin_short is True
    assert hit.shared == 120  # 余额只有这么多，就分这么多
    assert await coins_of(database, "c") == 0
    assert await coins_of(database, "a") == 50 + 120


async def test_solo_player_gets_no_share(database):
    await seed_coins(database, "a", 1000)
    service = make_service(database, chamber=1)
    await service.start(identity(user_id="a"))

    hit = await service.shoot(identity(user_id="a"), platform=StubPlatform())

    assert hit.kind == SHOT_HIT
    assert hit.shared == 0 and hit.sharers == 0
    assert await coins_of(database, "a") == 1000 - 500


async def test_the_victim_never_shares_their_own_penalty(database):
    """中弹的人不分钱 —— 哪怕他前面也开过枪，那 500 也不会流回他自己手里。"""
    await seed_coins(database, "a", 1000)
    service = make_service(database, chamber=2)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    safe = await service.shoot(identity(user_id="a"), platform=platform)  # 第 1 枪空枪 +50
    assert (safe.kind, safe.reward) == (SHOT_MISS, 50)

    hit = await service.shoot(identity(user_id="a"), platform=platform)  # 第 2 枪响

    assert hit.kind == SHOT_HIT
    assert hit.paid == 500
    assert hit.sharers == 0 and hit.shared == 0  # 只有他自己开过枪 → 没人可分
    # 之前空枪赚的 50 不会被扣回，只是不分账
    assert await coins_of(database, "a") == 1000 + 50 - 500


async def test_victim_is_excluded_even_when_others_shot(database):
    """有人一起开过枪时，中弹的那个人同样不参与分账。"""
    await seed_coins(database, "a", 1000)
    service = make_service(database, chamber=3)
    platform = StubPlatform()
    await service.start(identity(user_id="b"))

    await service.shoot(identity(user_id="a"), platform=platform)  # 第 1 枪 a +50
    await service.shoot(identity(user_id="b", nickname="乙"), platform=platform)  # 第 2 枪 b +100

    hit = await service.shoot(identity(user_id="a"), platform=platform)  # 第 3 枪 a 中弹

    assert hit.kind == SHOT_HIT
    assert hit.sharers == 1
    assert hit.share_each == 500  # 500 全给 b，一分不给中弹的 a
    assert hit.share_names == ("乙",)
    assert await coins_of(database, "a") == 1000 + 50 - 500
    assert await coins_of(database, "b") == 100 + 500


async def test_admin_victim_is_not_muted(database):
    await seed_coins(database, "c", 1000)
    service = make_service(database, chamber=1)
    platform = StubPlatform(roles={"c": "owner"})
    await service.start(identity(user_id="a"))

    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.muted is False
    assert hit.mute_blocked is True
    assert hit.mute_seconds == 60  # 记录本应禁言多久，供卡片说明
    assert platform.mute_calls == []
    assert await coins_of(database, "c") == 500


async def test_mute_failure_still_costs_coins(database):
    await seed_coins(database, "c", 1000)
    service = make_service(database, chamber=1)
    platform = StubPlatform(mute_ok=False)
    await service.start(identity(user_id="a"))

    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.muted is False
    assert hit.mute_blocked is False
    assert await coins_of(database, "c") == 500


async def test_ledger_records_every_movement(database):
    await seed_coins(database, "c", 1000)
    service = make_service(database, chamber=3)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="a"), platform=platform)
    await service.shoot(identity(user_id="b"), platform=platform)
    await service.shoot(identity(user_id="c"), platform=platform)

    rows = await database.fetchall(
        "SELECT reason, COUNT(*) AS total, COALESCE(SUM(delta), 0) AS amount "
        "FROM coin_ledger WHERE reason LIKE 'roulette%' GROUP BY reason ORDER BY reason",
    )
    summary = {row["reason"]: (int(row["total"]), int(row["amount"])) for row in rows}
    assert summary[REASON_SAFE] == (2, 150)  # a +50、b +100
    assert summary[REASON_HIT] == (1, -500)
    assert summary[REASON_SHARE] == (2, 500)  # 各 +250

    # 全局账目仍然自洽
    for user_id in ("a", "b", "c"):
        row = await database.fetchone("SELECT id FROM players WHERE user_id = ?", (user_id,))
        coins, _total, ledger = await service.players.coins_invariant_in_db(int(row["id"]))
        assert ledger == coins, user_id


# ---------------------------------------------------------------------------
# 子弹那一枪：炸膛白拿 / 中弹与超时
# ---------------------------------------------------------------------------


async def test_the_bullet_shot_can_misfire(database):
    await seed_coins(database, "f", 1000)
    # 弹槽 6：第 6 枪才响；这一枪掷出 0.05 < 25% → 炸膛，白拿
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(6, 0.05),
        clock=StubClock(),
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    outcome = None
    for index in range(6):
        outcome = await service.shoot(
            identity(user_id=f"p{index}"),
            platform=platform,
        )

    assert outcome is not None
    assert outcome.kind == SHOT_MISFIRE
    assert outcome.shots == 6
    assert outcome.reward == 500
    assert platform.mute_calls == []
    assert await coins_of(database, "f") == 1000
    assert await coins_of(database, "p0") == 50  # 前面几枪照样发奖（第一枪 50）
    assert await coins_of(database, "p4") == 250  # 第 5 枪 250
    assert await coins_of(database, "p5") == 500  # 炸膛这一枪白拿 500


async def test_misfire_rewards_are_recorded_in_the_ledger(database):
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(6, 0.05),
        clock=StubClock(),
    )
    await service.start(identity(user_id="a"))
    for index in range(6):
        await service.shoot(identity(user_id=f"p{index}"), platform=StubPlatform())

    row = await database.fetchone(
        "SELECT COUNT(*) AS total, COALESCE(SUM(delta), 0) AS amount "
        "FROM coin_ledger WHERE reason = ?",
        (REASON_MISFIRE,),
    )
    assert (int(row["total"]), int(row["amount"])) == (1, 500)


async def test_the_bullet_shot_hits_when_not_misfiring(database):
    await seed_coins(database, "p5", 1000)
    # 第 6 枪掷出 0.9 ≥ 25% → 正常中弹
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(6, 0.9),
        clock=StubClock(),
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    for index in range(5):
        await service.shoot(identity(user_id=f"p{index}"), platform=platform)
    outcome = await service.shoot(identity(user_id="p5"), platform=platform)

    assert outcome.kind == SHOT_HIT
    assert outcome.mute_seconds == 60
    assert await coins_of(database, "p5") == 500


async def test_misfire_percent_zero_never_misfires(database):
    await seed_coins(database, "p5", 1000)
    settings = load_settings({"roulette": {"misfire_percent": 0}})
    service = RouletteService(
        database,
        settings,
        rng=StubRng(6, 0.0),
        clock=StubClock(),
    )
    await service.start(identity(user_id="a"))
    for index in range(5):
        await service.shoot(identity(user_id=f"p{index}"), platform=StubPlatform())
    outcome = await service.shoot(identity(user_id="p5"), platform=StubPlatform())
    assert outcome.kind == SHOT_HIT


async def test_misfire_rewards_the_only_shooter_with_a_single_chamber(database):
    """只有 1 个弹槽：第一枪就是子弹那一枪，掷到炸膛就直接白拿。"""
    service = RouletteService(
        database,
        load_settings({"roulette": {"chambers": 1}}),
        rng=StubRng(1, 0.1),
        clock=StubClock(),
    )
    await service.start(identity(user_id="a"))
    outcome = await service.shoot(identity(user_id="a"), platform=StubPlatform())

    assert outcome.kind == SHOT_MISFIRE
    assert await coins_of(database, "a") == 500


@pytest.mark.parametrize("chamber", [1, 2, 3, 4, 5, 6])
async def test_misfire_can_happen_at_any_chamber(database, chamber: int):
    """炸膛判定跟着**子弹那一枪**走，不是只认最后一个弹槽。"""
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(chamber, 0.05),  # 5% < 25% → 炸膛
        clock=StubClock(),
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    outcome = None
    for index in range(chamber):
        outcome = await service.shoot(identity(user_id=f"p{index}"), platform=platform)

    assert outcome is not None
    assert (outcome.kind, outcome.shots) == (SHOT_MISFIRE, chamber)
    assert outcome.reward == 500
    assert platform.mute_calls == []
    assert await coins_of(database, f"p{chamber - 1}") == 500


@pytest.mark.parametrize("chamber", [2, 3, 6])
async def test_no_misfire_means_a_normal_hit_at_that_chamber(database, chamber: int):
    """同样是子弹那一枪，掷到 87.5% 那一侧就是正常中弹。"""
    victim = f"p{chamber - 1}"
    await seed_coins(database, victim, 1000)
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(chamber, 0.9),
        clock=StubClock(),
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    outcome = None
    for index in range(chamber):
        outcome = await service.shoot(identity(user_id=f"p{index}"), platform=platform)

    assert outcome is not None
    assert (outcome.kind, outcome.shots) == (SHOT_HIT, chamber)
    assert outcome.paid == 500
    assert await coins_of(database, victim) == 500


async def test_round_expires_after_the_timeout(database):
    clock = StubClock()
    service = make_service(database, chamber=3, clock=clock)
    await service.start(identity(user_id="a"))

    clock.advance(301)
    assert service.active_round(identity(user_id="a")) is None

    outcome = await service.shoot(identity(user_id="a"), platform=StubPlatform())
    assert outcome.kind == SHOT_NO_ROUND
    # 过期后可以重开
    again = await service.start(identity(user_id="b"))
    assert again.started is True


async def test_shooting_refreshes_the_timeout(database):
    clock = StubClock()
    service = make_service(database, chamber=4, clock=clock)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    clock.advance(200)
    await service.shoot(identity(user_id="a"), platform=platform)
    clock.advance(200)  # 距上次开枪 200 秒 < 300
    assert service.active_round(identity(user_id="a")) is not None


async def test_hit_ends_the_round(database):
    await seed_coins(database, "b", 1000)
    service = make_service(database, chamber=1)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))

    await service.shoot(identity(user_id="b"), platform=platform)

    assert service.active_round(identity(user_id="a")) is None
    assert (await service.shoot(identity(user_id="b"), platform=platform)).kind == SHOT_NO_ROUND
    assert (await service.start(identity(user_id="a"))).started is True


# ---------------------------------------------------------------------------
# 救援
# ---------------------------------------------------------------------------


async def test_rescue_unbans_with_duration_zero(database):
    service = RouletteService(database, load_settings({}), rng=StubRng(0.9), clock=StubClock())
    platform = StubPlatform()

    outcome = await service.rescue(identity(user_id="a"), "b", platform=platform)

    assert outcome.kind == "rescued"
    assert platform.mute_calls == [("b", 0)]


async def test_rescue_can_misfire(database):
    # 0.05 < 12.5% → 救援失败，不调接口
    service = RouletteService(database, load_settings({}), rng=StubRng(0.05), clock=StubClock())
    platform = StubPlatform()

    outcome = await service.rescue(identity(user_id="a"), "b", platform=platform)

    assert outcome.kind == "misfire"
    assert platform.mute_calls == []


async def test_rescue_reports_failure(database):
    service = make_service(database, chamber=1, values=(0.9,))
    platform = StubPlatform(mute_ok=False)

    outcome = await service.rescue(identity(user_id="a"), "b", platform=platform)

    assert outcome.kind == "failed"


async def test_rescue_outcome_is_serializable_shape():
    outcome = RescueOutcome(kind="rescued", target="b", target_name="龟乙")
    assert outcome.kind == "rescued" and outcome.target == "b"


# ---------------------------------------------------------------------------
# 补一枪（照搬 Pallas-Bot 的 judgment：只能补中弹被禁言的人）
# ---------------------------------------------------------------------------


async def hit_someone(database, *, chamber: int = 1, victim: str = "b", roles=None):
    """跑一局，让 ``victim`` 中弹被禁言，返回 (service, platform)。"""
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(chamber, 0.9),
        clock=StubClock(),
    )
    platform = StubPlatform(roles=roles)
    await service.start(identity(user_id="a"))
    for index in range(chamber):
        shooter = victim if index == chamber - 1 else f"p{index}"
        await service.shoot(identity(user_id=shooter, nickname=shooter), platform=platform)
    platform.mute_calls.clear()
    return service, platform


async def test_judgment_extends_the_victims_mute(database):
    service, platform = await hit_someone(database, victim="龟乙")

    outcome = await service.judgment(
        identity(user_id="a"),
        (("龟乙", "龟乙"),),
        platform=platform,
    )

    assert outcome.kind == "punished"
    assert outcome.names == ("龟乙",)
    assert outcome.seconds == 60
    assert platform.mute_calls == [("龟乙", 60)]


async def test_judgment_records_stack_on_the_original_mute(database):
    """追加禁言是在原有禁言上叠加，不是重置。"""
    clock = StubClock()
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(1),
        clock=clock,
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    before = service.punished(identity(user_id="a"))
    assert [(item.user_id, item.until) for item in before] == [("龟乙", 1060.0)]

    await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)

    after = service.punished(identity(user_id="a"))
    assert [(item.user_id, item.until) for item in after] == [("龟乙", 1120.0)]


async def test_judgment_only_targets_people_who_got_shot(database):
    service, platform = await hit_someone(database, victim="龟乙")

    outcome = await service.judgment(
        identity(user_id="a"),
        (("路人", "路人"),),
        platform=platform,
    )

    assert outcome.kind == "not_punished"
    assert platform.mute_calls == []


async def test_judgment_without_a_target_hits_everyone_on_the_list(database):
    service = RouletteService(
        database,
        load_settings({}),
        rng=StubRng(1, 0.9),
        clock=StubClock(),
    )
    platform = StubPlatform()
    # 一局只会有一个中弹的人，所以名单要攒两个就得开两局
    for victim in ("龟乙", "龟丙"):
        await service.start(identity(user_id="a"))
        await service.shoot(identity(user_id=victim, nickname=victim), platform=platform)
    platform.mute_calls.clear()

    outcome = await service.judgment(identity(user_id="a"), (), platform=platform)

    assert outcome.kind == "punished"
    assert sorted(outcome.names) == ["龟丙", "龟乙"]
    assert platform.mute_calls == [("龟乙", 60), ("龟丙", 60)]


async def test_judgment_can_jam(database):
    """12.5% 卡壳：什么都不发生（原版的 fail_prob）。"""
    service, platform = await hit_someone(database)
    service.rng = StubRng(0.05)  # 5% < 12.5% → 卡壳

    outcome = await service.judgment(
        identity(user_id="a"),
        (("b", "b"),),
        platform=platform,
    )

    assert outcome.kind == "fail"
    assert platform.mute_calls == []


class AlwaysDrunk:
    """假装群里正醉着（醉酒联动的门槛在 tests/test_drunk.py 里单独测）。"""

    async def state(self, identity):  # noqa: ANN001, ANN201 - 鸭子类型
        del identity
        return DrunkState(source="test", seconds=300)


async def test_judgment_can_backfire_on_the_requester(database):
    """反噬：这一枪补到发起者自己头上（只在醉酒时判定，原版语义）。"""
    service, platform = await hit_someone(database)
    service.drunk = AlwaysDrunk()
    service.rng = StubRng(0.9, 0.05)  # 不卡壳，但 5% < 12.5% → 反噬

    outcome = await service.judgment(
        identity(user_id="a", nickname="龟甲"),
        (("b", "b"),),
        platform=platform,
    )

    assert outcome.kind == "backfire"
    assert outcome.names == ("龟甲",)
    assert platform.mute_calls == [("a", 60)]
    assert [item.user_id for item in service.punished(identity(user_id="a"))] == [
        "b",
        "a",
    ]


async def test_judgment_skips_a_target_who_became_an_admin(database):
    """名单里的人后来当上了管理员 —— 禁不了，只能放弃（原版同样跳过）。"""
    service, platform = await hit_someone(database, victim="b")
    platform._roles["b"] = "owner"  # noqa: SLF001 - 模拟"中弹之后升官了"

    outcome = await service.judgment(
        identity(user_id="a"),
        (("b", "b"),),
        platform=platform,
    )

    assert outcome.kind == "protected"
    assert platform.mute_calls == []


async def test_judgment_backfire_on_an_admin_requester_is_blocked(database):
    """反噬撞上管理员：这一枪补不动，谁都不挨罚（原版的 protected 分支）。"""
    service, platform = await hit_someone(database, victim="b", roles={"a": "admin"})
    service.drunk = AlwaysDrunk()
    service.rng = StubRng(0.9, 0.05)  # 不卡壳，但反噬

    outcome = await service.judgment(
        identity(user_id="a", nickname="龟甲"),
        (("b", "b"),),
        platform=platform,
    )

    assert outcome.kind == "protected"
    assert platform.mute_calls == []


async def test_judgment_without_a_list_says_so(database):
    service = make_service(database, chamber=3)
    platform = StubPlatform()

    outcome = await service.judgment(identity(user_id="a"), (), platform=platform)

    assert outcome.kind == "no_one"
    assert platform.mute_calls == []


async def test_judgment_expires_with_the_mute(database):
    clock = StubClock()
    service = RouletteService(database, load_settings({}), rng=StubRng(1), clock=clock)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    clock.advance(61)
    platform.mute_calls.clear()
    outcome = await service.judgment(identity(user_id="a"), (), platform=platform)

    assert outcome.kind == "no_one"
    assert service.punished(identity(user_id="a")) == []


async def test_rescue_takes_the_victim_off_the_list(database):
    service, platform = await hit_someone(database, victim="龟乙")

    await service.rescue(identity(user_id="a"), "龟乙", platform=platform)

    assert service.punished(identity(user_id="a")) == []
    # 名单空了，谁都补不了
    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "no_one"


# ---------------------------------------------------------------------------
# 冷却：一个人不能连着补枪、也不能连着救（救人 / 补枪之间不互斥）
# ---------------------------------------------------------------------------


async def test_the_same_person_cannot_judge_twice_in_a_row(database):
    """连着补两枪：第二枪被冷却挡下，连接口都不调。"""
    clock = StubClock()
    service = RouletteService(
        database,
        load_settings({"roulette": {"mute_seconds": 300}}),
        rng=StubRng(1, 0.9, 0.9),
        clock=clock,
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)
    platform.mute_calls.clear()

    first = await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    assert first.kind == "punished"
    assert platform.mute_calls == [("龟乙", 60)]

    platform.mute_calls.clear()
    again = await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    assert again.kind == "cooldown"
    assert again.seconds == 60
    assert platform.mute_calls == []

    clock.advance(61)
    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "punished"


async def test_a_jammed_shot_still_burns_the_cooldown(database):
    """卡壳也算"补过一次"，不能立刻再来。"""
    clock = StubClock()
    service = RouletteService(database, load_settings({}), rng=StubRng(1, 0.9), clock=clock)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    service.rng = StubRng(0.05)  # 卡壳
    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "fail"

    service.rng = StubRng(0.9, 0.9)
    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "cooldown"


async def test_the_same_person_cannot_rescue_twice_in_a_row(database):
    """连着救：第二下被挡；救援失败（炸膛）一样要等。"""
    clock = StubClock()
    service = RouletteService(database, load_settings({}), rng=StubRng(1, 0.9), clock=clock)
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    service.rng = StubRng(0.05)  # 救援炸膛
    assert (await service.rescue(identity(user_id="a"), "龟乙", platform=platform)).kind == "misfire"

    service.rng = StubRng(0.9)
    blocked = await service.rescue(identity(user_id="a"), "龟乙", platform=platform)
    assert blocked.kind == "cooldown"
    assert blocked.seconds == 60
    assert platform.mute_calls == [("龟乙", 60)]  # 只有开局那次禁言

    clock.advance(61)
    assert (await service.rescue(identity(user_id="a"), "龟乙", platform=platform)).kind == "rescued"


async def test_rescue_and_judgment_do_not_block_each_other(database):
    """救和补不互斥：同一个人补完还能救，救完还能补。"""
    clock = StubClock()
    service = RouletteService(
        database,
        load_settings({"roulette": {"mute_seconds": 300}}),
        rng=StubRng(1, 0.9, 0.9),
        clock=clock,
    )
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "punished"
    # 紧接着救人：不受补枪冷却影响
    assert (await service.rescue(identity(user_id="a"), "龟乙", platform=platform)).kind == "rescued"

    # 再补：这里出的是"补枪自己的"冷却（不是"因为刚救过"）
    again = await service.judgment(identity(user_id="a"), (), platform=platform)
    assert again.kind == "cooldown"

    # 冷却过了再补一次，照样能补（人重新中弹进名单）
    clock.advance(61)
    service.rng = StubRng(1, 0.9, 0.9)
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)
    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "punished"


async def test_another_person_can_rescue_the_same_target(database):
    """多人可以轮流救同一个人。"""
    service, platform = await hit_someone(database, victim="龟乙")
    service.rng = StubRng(0.05)  # 甲救失败

    assert (await service.rescue(identity(user_id="a"), "龟乙", platform=platform)).kind == "misfire"
    # 换乙来救：没有冷却
    service.rng = StubRng(0.9)
    assert (
        await service.rescue(identity(user_id="b", nickname="乙"), "龟乙", platform=platform)
    ).kind == "rescued"


async def test_another_person_can_judge_the_same_target(database):
    """补枪的冷却也是按人算的。"""
    service, platform = await hit_someone(database, victim="龟乙")
    service.rng = StubRng(0.9, 0.9)

    first = await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    second = await service.judgment(
        identity(user_id="b", nickname="乙"),
        (("龟乙", "龟乙"),),
        platform=platform,
    )

    assert (first.kind, second.kind) == ("punished", "punished")


async def test_an_impossible_attempt_does_not_burn_the_cooldown(database):
    """名单空 / 点错人这种"没得做"的情况不吃冷却。"""
    service = make_service(database, chamber=3)
    platform = StubPlatform()

    assert (await service.judgment(identity(user_id="a"), (), platform=platform)).kind == "no_one"

    service._remember_punished(  # noqa: SLF001 - 造状态：名单里有人了
        service.key_for(identity(user_id="a")),
        "龟乙",
        "龟乙",
        60,
    )
    assert (
        await service.judgment(identity(user_id="a"), (("路人", "路人"),), platform=platform)
    ).kind == "not_punished"

    # 冷却没被消耗，来真的时候照样补得动
    service.rng = StubRng(0.9, 0.9)
    assert (
        await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    ).kind == "punished"


async def test_the_cooldown_can_be_turned_off(database):
    service, platform = await hit_someone(database, victim="龟乙")
    service.settings = load_settings({"roulette": {"action_cooldown_seconds": 0}})
    service.rng = StubRng(0.9, 0.9)

    first = await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)
    second = await service.judgment(identity(user_id="a"), (("龟乙", "龟乙"),), platform=platform)

    assert (first.kind, second.kind) == ("punished", "punished")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


async def test_numbers_are_configurable(database):
    settings = load_settings(
        {
            "roulette": {
                "chambers": 2,
                "safe_reward": 10,
                "hit_penalty": 100,
                "share_percent": 100,
                "mute_seconds": 60,
            },
        },
    )
    await seed_coins(database, "c", 100)
    service = RouletteService(database, settings, rng=StubRng(2), clock=StubClock())
    platform = StubPlatform()
    start = await service.start(identity(user_id="a"))
    assert (start.chambers, start.safe_reward, start.hit_penalty, start.mute_seconds) == (
        2,
        10,
        100,
        60,
    )

    await service.shoot(identity(user_id="a"), platform=platform)  # +10
    hit = await service.shoot(identity(user_id="c"), platform=platform)

    assert hit.paid == 100
    assert hit.shared == 100  # share_percent=100
    assert await coins_of(database, "a") == 110
    assert platform.mute_calls == [("c", 60)]


async def test_disabled_roulette_is_rejected_by_config(database):
    settings = load_settings({"roulette": {"enabled": False}})
    assert settings.roulette.enabled is False
    service = RouletteService(database, settings, rng=StubRng(1), clock=StubClock())
    # 服务层不管开关（由 main.py 决定），这里确认配置能读到
    assert (await service.start(identity(user_id="a"))).started is True


@pytest.mark.parametrize("chambers", [1, 2, 6, 10])
async def test_first_shot_hits_when_chamber_is_one(database, chambers: int):
    await seed_coins(database, "a", 1000)
    service = make_service(database, chamber=1, settings=load_settings({"roulette": {"chambers": chambers}}))
    platform = StubPlatform()
    await service.start(identity(user_id="a"))
    outcome = await service.shoot(identity(user_id="a"), platform=platform)
    assert outcome.kind == SHOT_HIT
    assert await coins_of(database, "a") == 500


def test_mention_helper_is_available_for_rescue():
    """救援要取被 @ 的人（这里只确认 conftest 的假组件可用）。"""
    assert FakeAt("201", "龟乙").qq == "201"
