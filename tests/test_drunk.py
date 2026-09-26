"""醉酒联动：从别的插件（喝酒插件）读"这个群醉没醉"。

真实来源是 `astrbot_plugin_repeater` 的 ``RepeaterEngine.drunk_remaining(group_key)``
（状态只在内存里，按 ``平台:群`` 存）。这里用假插件对象把各种形态都覆盖一遍。
"""

from __future__ import annotations

import pytest

from funhub.config import load_settings
from funhub.drunk import DrunkSource, DrunkState, group_key_for
from funhub.players import PlayerIdentity
from funhub.roulette import RouletteService

from .conftest import identity

REPEATER = "astrbot_plugin_repeater"


class FakeMetadata:
    """模仿 AstrBot 的 ``StarMetadata``：插件实例挂在 ``star_cls`` 上。"""

    def __init__(self, name: str, instance, *, activated: bool = True) -> None:
        self.root_dir_name = name
        self.name = name
        self.module_path = f"data.plugins.{name}.main"
        self.star_cls = instance
        self.activated = activated


class FakeRepeaterEngine:
    """模仿 ``RepeaterEngine``：``drunk_remaining`` 是同步方法。"""

    def __init__(self, *, remaining: int = 0, raises: Exception | None = None) -> None:
        self.remaining = remaining
        self.raises = raises
        self.calls: list[str] = []

    def drunk_remaining(self, group_key: str, *, now: int | None = None) -> int:
        del now
        self.calls.append(group_key)
        if self.raises is not None:
            raise self.raises
        return self.remaining


class FakeRepeaterPlugin:
    def __init__(self, engine: FakeRepeaterEngine) -> None:
        self.engine = engine


def source(*items, **kwargs) -> DrunkSource:
    return DrunkSource(lambda: list(items), **kwargs)


# ---------------------------------------------------------------------------
# 读取状态
# ---------------------------------------------------------------------------


def test_group_key_matches_the_drinking_plugin():
    """两边都是 ``平台:群``，所以直接能对上。"""
    assert group_key_for(identity(user_id="a")) == "aiocqhttp:100"


async def test_state_is_read_from_the_repeater_engine():
    engine = FakeRepeaterEngine(remaining=123)
    drunk = source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine)))

    state = await drunk.state(identity(user_id="a"))

    assert state.known is True
    assert state.drunk is True
    assert state.seconds == 123
    assert state.source == REPEATER
    assert engine.calls == ["aiocqhttp:100"]


async def test_sober_is_zero_seconds():
    engine = FakeRepeaterEngine(remaining=0)
    drunk = source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine)))

    state = await drunk.state(identity(user_id="a"))

    assert state.known is True
    assert state.drunk is False
    assert state.seconds == 0


async def test_missing_plugin_means_sober():
    drunk = source()
    state = await drunk.state(identity(user_id="a"))

    assert state.known is False
    assert state.drunk is False


async def test_other_plugins_are_ignored():
    drunk = source(FakeMetadata("astrbot_plugin_funhub", object()))

    assert await drunk.remaining(identity(user_id="a")) == 0


async def test_inactive_plugin_is_ignored():
    engine = FakeRepeaterEngine(remaining=99)
    drunk = source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine), activated=False))

    assert (await drunk.state(identity(user_id="a"))).known is False


async def test_broken_plugin_never_raises():
    """别人的插件抛异常，不能把轮盘带崩 —— 当作清醒即可。"""
    engine = FakeRepeaterEngine(raises=RuntimeError("repeater 炸了"))
    drunk = source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine)))

    assert (await drunk.state(identity(user_id="a"))).drunk is False
    assert (await drunk.remaining(identity(user_id="a"))) == 0


async def test_async_and_boolean_shapes_are_understood():
    class AsyncPlugin:
        async def drunk_remaining(self, group_key: str) -> int:
            del group_key
            return 42

    class BoolPlugin:
        def drunkenness(self) -> bool:
            return True

    assert (await source(FakeMetadata(REPEATER, AsyncPlugin())).state(identity())).seconds == 42
    assert (await source(FakeMetadata(REPEATER, BoolPlugin())).state(identity())).drunk is True


async def test_a_plugin_that_takes_no_argument_still_works():
    class NoArgPlugin:
        def drunkenness(self) -> int:
            return 7

    state = await source(FakeMetadata(REPEATER, NoArgPlugin())).state(identity())

    assert state.drunk is True and state.seconds == 7


async def test_a_stars_provider_that_explodes_is_survivable():
    def boom():
        raise RuntimeError("宿主接口异常")

    assert (await DrunkSource(boom).state(identity())).known is False


# ---------------------------------------------------------------------------
# 接进补一枪：反噬门槛
# ---------------------------------------------------------------------------


def make_service(database, *, drunk_seconds: int, drunk_only: bool = True, values=()):
    class Rng:
        def __init__(self, items):
            self.items = list(items)

        def randint(self, low, high):  # pragma: no cover - 本文件用不到
            return low

        def random(self):
            return float(self.items.pop(0)) if self.items else 0.99

    engine = FakeRepeaterEngine(remaining=drunk_seconds)
    service = RouletteService(
        database,
        load_settings(
            {"roulette": {"judgment_backfire_drunk_only": drunk_only}},
        ),
        rng=Rng(values),
        clock=lambda: 0.0,
        drunk=source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine))),
    )
    return service


class Platform:
    def __init__(self) -> None:
        self.mute_calls: list[tuple[str, int]] = []

    async def member_role(self, user_id: str) -> str:  # pragma: no cover - 简化
        return "member"

    async def mute(self, user_id: str, seconds: int):
        from funhub.platform_api import MuteOutcome

        self.mute_calls.append((user_id, seconds))
        return MuteOutcome(ok=True)


async def put_on_the_list(service: RouletteService, *, victim: str = "龟乙") -> None:
    """直接把人记进"中弹被禁言"名单（跳过一整局）。"""
    service._remember_punished(  # noqa: SLF001 - 这里就是要造这个状态
        service.key_for(identity(user_id="a")),
        victim,
        victim,
        60,
    )


async def test_sober_means_no_backfire(database):
    """照搬原版：清醒时补一枪没有风险（只剩 12.5% 卡壳）。"""
    service = make_service(database, drunk_seconds=0, values=(0.9, 0.05))
    await put_on_the_list(service)
    platform = Platform()

    outcome = await service.judgment(
        identity(user_id="a", nickname="龟甲"),
        (("龟乙", "龟乙"),),
        platform=platform,
    )

    assert outcome.kind == "punished"
    assert platform.mute_calls == [("龟乙", 60)]


async def test_drunk_can_backfire(database):
    service = make_service(database, drunk_seconds=300, values=(0.9, 0.05))
    await put_on_the_list(service)
    platform = Platform()

    outcome = await service.judgment(
        identity(user_id="a", nickname="龟甲"),
        (("龟乙", "龟乙"),),
        platform=platform,
    )

    assert outcome.kind == "backfire"
    assert platform.mute_calls == [("a", 60)]


async def test_drunk_without_the_roll_still_hits_the_target(database):
    service = make_service(database, drunk_seconds=300, values=(0.9, 0.9))
    await put_on_the_list(service)
    platform = Platform()

    outcome = await service.judgment(
        identity(user_id="a", nickname="龟甲"),
        (("龟乙", "龟乙"),),
        platform=platform,
    )

    assert outcome.kind == "punished"
    assert platform.mute_calls == [("龟乙", 60)]


async def test_drunk_only_can_be_turned_off(database):
    """关掉开关：没有喝酒插件也照样可能反噬（之前的行为）。"""
    service = make_service(database, drunk_seconds=0, drunk_only=False, values=(0.9, 0.05))
    await put_on_the_list(service)
    platform = Platform()

    outcome = await service.judgment(
        identity(user_id="a", nickname="龟甲"),
        (("龟乙", "龟乙"),),
        platform=platform,
    )

    assert outcome.kind == "backfire"


async def test_drunkenness_is_reported_for_diagnostics(database):
    service = make_service(database, drunk_seconds=88)
    state = await service.drunk_state(identity(user_id="a"))

    assert isinstance(state, DrunkState)
    assert state.seconds == 88 and state.drunk is True


async def test_service_without_a_drunk_source_is_sober(database):
    """没接醉酒来源（测试 / 独立使用）时不能反噬。"""
    service = RouletteService(database, load_settings({}), rng=None, clock=lambda: 0.0)
    await put_on_the_list(service)

    assert (await service.drunk_state(identity(user_id="a"))).drunk is False
    assert await service.may_backfire(identity(user_id="a")) is False


@pytest.mark.parametrize("seconds", [0, 1, 300])
async def test_may_backfire_follows_the_clock_of_the_source(database, seconds: int):
    service = make_service(database, drunk_seconds=seconds, values=(0.05,))

    assert await service.may_backfire(identity(user_id="a")) is (seconds > 0)


# ---------------------------------------------------------------------------
# 醉酒多杀：中弹的人连带几个同局开过枪的一起受罚
# ---------------------------------------------------------------------------


class RouletteRng:
    """轮盘用随机源：第一次 randint 定弹槽、之后那个是"醉酒拉几个人"。

    ``shuffle`` 按给定顺序（不给就不打乱），``random()`` 走脚本（默认 0.99 = 不炸膛）。
    """

    def __init__(
        self,
        *,
        chamber: int,
        randoms=(),
        shuffle_to=None,
        extra_count: int | None = None,
    ) -> None:
        self.chamber = chamber
        self.randoms = list(randoms)
        self.shuffle_to = list(shuffle_to or [])
        self.extra_count = extra_count
        self.randint_calls: list[tuple[int, int]] = []

    def randint(self, low: int, high: int) -> int:
        self.randint_calls.append((low, high))
        if len(self.randint_calls) == 1:  # 开局定弹槽
            return min(max(self.chamber, low), high)
        if self.extra_count is None:
            return high
        return min(max(self.extra_count, low), high)

    def random(self) -> float:
        return float(self.randoms.pop(0)) if self.randoms else 0.99

    def shuffle(self, items: list) -> None:
        items[:] = self.shuffle_to or items


async def seed_coins(database, user_id: str, coins: int) -> None:
    from funhub.players import PlayerRepository

    repository = PlayerRepository(database)
    async with database.transaction() as connection:
        player, _ = await repository.get_or_create_in_db(
            connection,
            identity(user_id=user_id, nickname=user_id),
        )
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


async def play_round(
    database,
    *,
    drunk_seconds: int,
    extra_max: int = 2,
    extra_count: int | None = 1,
    shooters=("甲", "乙"),
):
    """开一局：``shooters`` 先各开一枪（空枪），最后让龟乙中弹。"""
    engine = FakeRepeaterEngine(remaining=drunk_seconds)
    rng = RouletteRng(
        chamber=len(shooters) + 1,
        shuffle_to=list(shooters),
        extra_count=extra_count,
    )
    service = RouletteService(
        database,
        # 概率写死 100%，测试不依赖默认值
        load_settings({"roulette": {"drunk_extra_max": extra_max, "drunk_extra_percent": 100}}),
        rng=rng,
        clock=lambda: 0.0,
        drunk=source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine))),
    )
    platform = Platform()
    await service.start(identity(user_id="a"))
    for name in shooters:
        await service.shoot(identity(user_id=name, nickname=name), platform=platform)
    victim = "龟乙"
    await seed_coins(database, victim, 5000)
    outcome = await service.shoot(identity(user_id=victim, nickname=victim), platform=platform)
    return service, platform, outcome


async def test_sober_hit_punishes_only_the_shooter(database):
    service, platform, outcome = await play_round(database, drunk_seconds=0)

    assert outcome.drunk is False
    assert [item.name for item in outcome.victims] == ["龟乙"]
    assert platform.mute_calls == [("龟乙", 60)]


async def test_drunk_hit_drags_others_in(database):
    """醉酒：中弹的人 + 1~N 名同局开过枪的人一起受罚。"""
    service, platform, outcome = await play_round(database, drunk_seconds=300, extra_max=2)

    assert outcome.drunk is True
    names = [item.name for item in outcome.victims]
    assert names[0] == "龟乙"  # 中弹的排第一
    assert names == ["龟乙", "甲"]  # 脚本指定拉 1 个，shuffle 后是甲
    assert sorted(call[0] for call in platform.mute_calls) == ["甲", "龟乙"] or sorted(
        call[0] for call in platform.mute_calls
    ) == ["乙", "龟乙"]
    # 龟乙（被 seed 过 5000）全额 500；甲只在前面的空枪里赚了 50，按「尽所能」全给
    assert [item.paid for item in outcome.victims] == [500, 50]
    assert outcome.paid == 500  # 主受害者字段仍然可用
    assert await coins_of(database, "龟乙") == 4500
    assert await coins_of(database, "甲") == 0


async def test_drunk_extras_are_capped_by_the_config(database):
    service, platform, outcome = await play_round(
        database,
        drunk_seconds=300,
        extra_max=1,
        shooters=("甲", "乙", "丙"),
    )

    assert len(outcome.victims) == 2  # 中弹者 + 1 个陪罚


async def test_drunk_multi_kill_can_be_turned_off(database):
    service, platform, outcome = await play_round(database, drunk_seconds=300, extra_max=0)

    assert outcome.drunk is False
    assert [item.name for item in outcome.victims] == ["龟乙"]


@pytest.mark.parametrize(
    ("percent", "roll", "expect_extra"),
    [
        (100, 0.99, True),  # 默认：醉了必走火
        (50, 0.10, True),  # 掷到 10% < 50% → 走火
        (50, 0.90, False),  # 掷到 90% ≥ 50% → 没走火
        (0, 0.10, False),  # 关掉走火
    ],
)
async def test_misfire_probability(database, percent: float, roll: float, expect_extra: bool):
    """喝醉只是前提，走火本身还有概率（默认 100% = 原版行为）。"""
    engine = FakeRepeaterEngine(remaining=300)
    service = RouletteService(
        database,
        load_settings(
            {"roulette": {"drunk_extra_max": 2, "drunk_extra_percent": percent}},
        ),
        rng=RouletteRng(
            chamber=3,
            shuffle_to=["甲"],
            extra_count=1,
            # 第一个 0.99 给"这一枪炸不炸膛"，第二个才是"走不走火"
            randoms=(0.99, roll),
        ),
        clock=lambda: 0.0,
        drunk=source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine))),
    )
    platform = Platform()
    await seed_coins(database, "龟乙", 5000)
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="甲", nickname="甲"), platform=platform)
    await service.shoot(identity(user_id="丙", nickname="丙"), platform=platform)

    outcome = await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    assert outcome.drunk is expect_extra
    assert len(outcome.victims) == (2 if expect_extra else 1)


async def test_sober_never_misfires_even_at_full_probability(database):
    service, platform, outcome = await play_round(database, drunk_seconds=0)

    assert outcome.drunk is False


async def test_drunk_without_other_shooters_stays_single(database):
    service, platform, outcome = await play_round(database, drunk_seconds=300, shooters=())

    assert outcome.drunk is False
    assert [item.name for item in outcome.victims] == ["龟乙"]


async def test_drunk_pot_is_shared_by_those_who_are_not_hit(database):
    """陪罚者之间不互相退款：钱分给"开过枪但没中弹"的人。"""
    engine = FakeRepeaterEngine(remaining=300)
    service = RouletteService(
        database,
        load_settings({"roulette": {"drunk_extra_max": 1, "drunk_extra_percent": 100}}),
        rng=RouletteRng(chamber=3, shuffle_to=["甲"], extra_count=1),
        clock=lambda: 0.0,
        drunk=source(FakeMetadata(REPEATER, FakeRepeaterPlugin(engine))),
    )
    platform = Platform()
    for name in ("甲", "丙"):
        await seed_coins(database, name, 1000)
    await seed_coins(database, "龟乙", 1000)
    await service.start(identity(user_id="a"))
    await service.shoot(identity(user_id="甲", nickname="甲"), platform=platform)
    await service.shoot(identity(user_id="丙", nickname="丙"), platform=platform)

    outcome = await service.shoot(identity(user_id="龟乙", nickname="龟乙"), platform=platform)

    assert outcome.drunk is True
    assert {item.name for item in outcome.victims} == {"龟乙", "甲"}
    # 两人各赔 500（甲被 seed 的 1000 加上空枪的 50 够付）→ 1000 全给没中弹的丙
    assert outcome.share_names == ("丙",)
    assert outcome.share_each == 1000
    # 丙：1000 + 空枪 100 + 分账 1000
    assert await coins_of(database, "丙") == 2100
    assert await coins_of(database, "甲") == 1050 - 500
    assert await coins_of(database, "龟乙") == 500


async def test_drunk_victims_join_the_extra_shot_list(database):
    """陪罚的人也被禁言了，所以 /补一枪 也打得着他们。"""
    service, platform, outcome = await play_round(database, drunk_seconds=300)

    assert service.punished(identity(user_id="a")) != []
    assert {item.name for item in service.punished(identity(user_id="a"))} == {"龟乙", "甲"}


async def test_drunk_multi_kill_keeps_the_ledger_consistent(database):
    service, platform, outcome = await play_round(database, drunk_seconds=300)

    for user_id in ("甲", "龟乙", "a"):
        row = await database.fetchone("SELECT id FROM players WHERE user_id = ?", (user_id,))
        if row is None:
            continue
        coins, _total, ledger = await service.players.coins_invariant_in_db(int(row["id"]))
        assert ledger == coins, user_id
