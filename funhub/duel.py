"""决斗：一轮随机数定胜负，输的人掉龟龟币（主动挑事的人输了还会被禁言）。

沿用老插件的"决斗"玩法，但做了三处简化：

* 三轮随机数 → **一轮**，谁大谁赢；点数相同就**加赛**，直到分出胜负为止（没有平局）；
* 不看等级、属性、装备，纯运气；
* **没有冷却**，想打几次打几次。

奖惩（数值都可在配置里改）::

    无论谁输：败者赔 0~500 随机币 × 倍率给胜者（按"尽所能"：余额不够就全给）
    倍率 = 1 + 存款碾压 + 管理员败北
           存款碾压：败者每比胜者多 500 币多赔 10%，最多 +50%
           管理员败北：败者是群主/管理员（平台不允许禁言他们）
                        → 在普通人算出来的倍率上再加 0.2 倍，不是固定值
           两者加算 → 普通人最多 1.5 倍、管理员最多 1.7 倍
    主动挑事的人输了     → 额外被禁言 1 分钟（机器人不是管理员时没有这项）
    挑事者已经在禁言里   → 不叠加禁言，本次也免罚
    被打的人输了         → 只赔币，不会被禁言

**禁言只针对主动挑事的那一方**：被打的人永远不会被禁言；但"管理员多赔 0.2 倍"看的是
败者身份（管理员根本禁不了言，惩罚一律折成多赔钱），与谁先挑事无关。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import tzinfo

from . import messages
from .config import DuelSettings, Settings
from .db import Database
from .platform_api import (
    ADMIN_ROLES,
    REASON_ERROR,
    REASON_PROTECTED,
    REASON_UNSUPPORTED,
    GroupPlatform,
)
from .players import Player, PlayerIdentity, PlayerRepository, utc_after_text

#: 胜者 / 败者归属
SIDE_CHALLENGER = "challenger"
SIDE_TARGET = "target"

#: 结果形态
MODE_COIN = "coin"
MODE_COIN_AND_MUTE = "coin_and_mute"
MODE_IMMUNE = "immune"

#: 免疫原因（只剩"正在禁言中"这一种）
IMMUNE_MUTED = "already_muted"


@dataclass(frozen=True, slots=True)
class MuteState:
    """禁言这一步的结果。"""

    muted: bool = False
    failure: str = ""
    loser_is_admin: bool = False
    """败者是群主/管理员（平台不允许禁言他们）→ 赔付倍率再加 admin_percent%。"""

#: 加赛上限。正常随机源下平局概率是 1/(dice_max+1)，几乎不会加赛；
#: 这个上限只是防止有人把骰子配置成常数（或注入退化随机源）时死循环。
MAX_ROUNDS = 10


@dataclass(frozen=True, slots=True)
class DuelRound:
    """一回合的点数。"""

    challenger: int
    target: int

    @property
    def tied(self) -> bool:
        return self.challenger == self.target


@dataclass(frozen=True, slots=True)
class DuelRoll:
    """决斗的点数记录。``rounds`` 多于一条说明加赛过。"""

    rounds: tuple[DuelRound, ...]
    forced: bool = False
    """加赛到上限仍分不出胜负（骰子退化成常数），此时判挑战者胜。"""

    @property
    def final(self) -> DuelRound:
        return self.rounds[-1]

    @property
    def winner(self) -> str:
        last = self.final
        if last.challenger >= last.target:
            return SIDE_CHALLENGER
        return SIDE_TARGET

    @property
    def overtime(self) -> bool:
        return len(self.rounds) > 1


@dataclass(frozen=True, slots=True)
class PenaltyPlan:
    """一次赔付的构成。"""

    base: int
    rich_excess: int = 0
    rich_percent: int = 0
    admin_percent: int = 0

    @property
    def multiplier(self) -> float:
        return 1.0 + (self.rich_percent + self.admin_percent) / 100

    @property
    def total(self) -> int:
        """应赔总额（含倍率）。"""
        return round_half_up(self.base * self.multiplier)

    @property
    def boosted(self) -> bool:
        return bool(self.rich_percent or self.admin_percent)


def round_half_up(value: float) -> int:
    return math.floor(float(value) + 0.5)


def plan_penalty(
    duel: DuelSettings,
    *,
    base: int,
    loser_coins: int,
    winner_coins: int,
    loser_is_admin: bool,
) -> PenaltyPlan:
    """算出这次要赔多少。

    * 败者存款每比胜者多 ``rich_step_coins`` 币 → 多赔 ``rich_step_percent``%，
      最多 ``rich_cap_percent``%；
    * 败者是群主/管理员 → **在普通人算出来的倍率上再加 0.2 倍**
      （``admin_percent`` 默认 20，是加数而非固定倍率）；
    * 两部分加算，所以普通人最多 1.5 倍、管理员最多 1.7 倍（默认配置下）。
    """
    excess = max(int(loser_coins) - int(winner_coins), 0)
    step = max(int(duel.rich_step_coins), 1)
    steps = excess // step
    rich_percent = min(steps * duel.rich_step_percent, duel.rich_cap_percent)
    admin_percent = duel.admin_percent if loser_is_admin else 0
    return PenaltyPlan(
        base=max(int(base), 0),
        rich_excess=excess,
        rich_percent=rich_percent,
        admin_percent=admin_percent,
    )


@dataclass(frozen=True, slots=True)
class DuelResult:
    challenger: Player
    target: Player
    roll: DuelRoll
    mode: str
    winner: Player
    loser: Player
    penalty: PenaltyPlan = field(default_factory=lambda: PenaltyPlan(base=0))
    paid: int = 0
    mute_seconds: int = 0
    muted: bool = False
    mute_failure: str = ""
    immune_reason: str = ""

    @property
    def stake(self) -> int:
        """应赔总额（含倍率）。"""
        return self.penalty.total

    @property
    def transferred(self) -> int:
        """败者实际付出的龟龟币（受余额限制）。"""
        if self.mode == MODE_IMMUNE:
            return 0
        return self.paid


def roll_once(rng: random.Random | None, *, dice_max: int) -> DuelRound:
    """掷一回合：双方各一个 ``0..dice_max``。"""
    source = rng or random
    top = max(int(dice_max), 1)
    return DuelRound(
        challenger=source.randint(0, top),
        target=source.randint(0, top),
    )


def roll_duel(
    rng: random.Random | None,
    *,
    dice_max: int,
    max_rounds: int = MAX_ROUNDS,
) -> DuelRoll:
    """掷到分出胜负为止。点数相同就加赛，所以不会有平局。"""
    rounds: list[DuelRound] = []
    for _ in range(max(int(max_rounds), 1)):
        current = roll_once(rng, dice_max=dice_max)
        rounds.append(current)
        if not current.tied:
            return DuelRoll(tuple(rounds))
    return DuelRoll(tuple(rounds), forced=True)


def _duration_text(seconds: int) -> str:
    seconds = int(seconds)
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _rolls_text(roll: DuelRoll, *, max_shown: int = 3) -> str:
    """``50 : 50 → 90 : 10``：加赛过的回合按顺序排开。

    加赛很多次时（骰子配置得很小才会发生）只留首尾，中间用省略号，避免卡片被撑爆。
    """
    rounds = roll.rounds
    if len(rounds) > max_shown:
        rounds = (rounds[0], rounds[-1])
        joiner = " → … → "
    else:
        joiner = " → "
    return joiner.join(f"{item.challenger} : {item.target}" for item in rounds)


class DuelService:
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

    # ---------------------------------------------------------------- 用例

    async def duel(
        self,
        challenger_identity: PlayerIdentity,
        target_identity: PlayerIdentity,
        *,
        platform: GroupPlatform,
        zone: tzinfo,
    ) -> DuelResult:
        """跑一场决斗。"""
        duel_settings = self.settings.duel
        async with self.database.transaction() as connection:
            challenger, _ = await self.players.get_or_create_in_db(
                connection,
                challenger_identity,
            )
            target, _ = await self.players.get_or_create_in_db(
                connection,
                target_identity,
                nickname=target_identity.nickname or None,
            )

        roll = roll_duel(self.rng, dice_max=duel_settings.dice_max)
        winner = challenger if roll.winner == SIDE_CHALLENGER else target
        loser = target if roll.winner == SIDE_CHALLENGER else challenger

        # 豁免只针对"本来就该被禁言的那一方"，也就是主动挑事者：他已经在禁言里了就不再
        # 叠加，本次也免罚。被打的人不受这条影响 —— 他本来就不会被禁言，照常赔币。
        if roll.winner == SIDE_TARGET and await self._is_already_muted(
            loser,
            platform=platform,
        ):
            return DuelResult(
                challenger=challenger,
                target=target,
                roll=roll,
                mode=MODE_IMMUNE,
                winner=winner,
                loser=loser,
                immune_reason=IMMUNE_MUTED,
            )

        base_stake = self._roll_stake(duel_settings.stake_max)
        mute_state = await self._mute_punishment(
            roll=roll,
            loser=loser,
            platform=platform,
        )
        # 管理员加成：平台明确查到角色就用它；查不到时以"禁言失败且对方受保护"为准
        loser_is_admin = mute_state.loser_is_admin
        penalty = plan_penalty(
            duel_settings,
            base=base_stake,
            loser_coins=loser.coins,
            winner_coins=winner.coins,
            loser_is_admin=loser_is_admin,
        )

        mode = MODE_COIN_AND_MUTE if mute_state.muted else MODE_COIN
        paid = await self._settle(
            winner_pk=winner.id,
            loser_pk=loser.id,
            amount=penalty.total,
            winner_ref=loser.user_id,
            loser_ref=winner.user_id,
            muted=mute_state.muted,
            mute_seconds=duel_settings.mute_seconds,
        )

        async with self.database.transaction() as connection:
            challenger_after = await self.players.reload_in_db(connection, challenger.id)
            target_after = await self.players.reload_in_db(connection, target.id)

        winner_after = (
            challenger_after if roll.winner == SIDE_CHALLENGER else target_after
        )
        loser_after = target_after if roll.winner == SIDE_CHALLENGER else challenger_after
        return DuelResult(
            challenger=challenger_after,
            target=target_after,
            roll=roll,
            mode=mode,
            winner=winner_after,
            loser=loser_after,
            penalty=penalty,
            paid=paid,
            mute_seconds=duel_settings.mute_seconds if mute_state.muted else 0,
            muted=mute_state.muted,
            mute_failure=mute_state.failure,
        )

    # ---------------------------------------------------------------- 内部

    async def _mute_punishment(
        self,
        *,
        roll: DuelRoll,
        loser: Player,
        platform: GroupPlatform,
    ) -> MuteState:
        """禁言惩罚。

        **只有主动挑事的人输了才会被禁言**（``roll.winner == SIDE_TARGET`` 表示挑战者输）；
        被打的人输了只赔币。管理员/群主本来就禁不了言，这里顺手把这件事查出来，
        供赔付倍率使用（他们的惩罚一律折算成"倍率再加 0.2"）。
        """
        loser_role = await platform.member_role(loser.user_id)
        loser_is_admin = loser_role in ADMIN_ROLES

        if roll.winner != SIDE_TARGET:
            return MuteState(loser_is_admin=loser_is_admin)

        if loser_is_admin:
            # 平台明确说对方是管理员/群主：禁不了，不必白调一次接口
            return MuteState(loser_is_admin=True)

        bot_role = await platform.bot_role()
        if bot_role is not None and bot_role not in ADMIN_ROLES:
            # 机器人不是管理员：没有禁言这项惩罚
            return MuteState()

        outcome = await platform.mute(loser.user_id, self.settings.duel.mute_seconds)
        if outcome.ok:
            return MuteState(muted=True)

        reason = outcome.reason or (
            REASON_ERROR if bot_role in ADMIN_ROLES else REASON_UNSUPPORTED
        )
        # 角色查不到（QQ 官方机器人）时，用错误归类兜底判断"对方不能被禁言"
        protected = reason == REASON_PROTECTED and loser_role is None
        return MuteState(failure=reason, loser_is_admin=protected)

    async def _is_already_muted(
        self,
        loser: Player,
        *,
        platform: GroupPlatform,
    ) -> bool:
        """败者是否已经在禁言中（平台查得到就用平台的，否则看我们自己的记录）。"""
        if await platform.is_muted(loser.user_id):
            return True
        return bool(await self.players.muted_until(loser.id))

    def _roll_stake(self, stake_max: int) -> int:
        top = max(int(stake_max), 0)
        if top == 0:
            return 0
        source = self.rng or random
        return source.randint(0, top)

    async def _settle(
        self,
        *,
        winner_pk: int,
        loser_pk: int,
        amount: int,
        winner_ref: str,
        loser_ref: str,
        muted: bool,
        mute_seconds: int,
    ) -> int:
        """在一个事务里完成扣款、转账与禁言记录，返回败者实际付出的币。

        ``amount`` 只是"应赔"，实际以败者余额为上限（尽所能）。
        """
        async with self.database.transaction() as connection:
            loser_before = await self.players.reload_in_db(connection, loser_pk)
            if amount > 0:
                await self.players.apply_coin_delta_in_db(
                    connection,
                    player_pk=loser_pk,
                    delta=-amount,
                    reason="duel_lose",
                    ref=winner_ref,
                )
                loser_after = await self.players.reload_in_db(connection, loser_pk)
                paid = loser_before.coins - loser_after.coins
                if paid:
                    await self.players.apply_coin_delta_in_db(
                        connection,
                        player_pk=winner_pk,
                        delta=paid,
                        reason="duel_win",
                        ref=loser_ref,
                    )
            else:
                paid = 0
            if muted:
                # 记录用 UTC，方便和 utc_now_text() 直接做字符串比较；
                # 平台侧需要本地时间的到期时刻，由各平台实现自己换算。
                await self.players.record_mute_in_db(
                    connection,
                    player_pk=loser_pk,
                    expires_at=utc_after_text(mute_seconds),
                )
        return paid

    # ---------------------------------------------------------------- 渲染

    def render(self, result: DuelResult) -> str:
        return messages.duel_card(
            challenger=result.challenger.display_name,
            target=result.target.display_name,
            rolls_text=_rolls_text(result.roll),
            mode=result.mode,
            winner=result.winner.display_name,
            loser=result.loser.display_name,
            gain=result.transferred,
            penalty_note=_penalty_note(result),
            mute_failure=result.mute_failure,
            immune_reason=result.immune_reason,
            duration_text=_duration_text(result.mute_seconds),
        )


def _penalty_note(result: DuelResult) -> str:
    """倍率说明：管理员加成优先说明（它包含存款碾压那一份）。"""
    penalty = result.penalty
    if not penalty.boosted:
        return ""
    if penalty.admin_percent:
        return f"{result.loser.display_name} 是管理员，赔付 ×{penalty.multiplier:.1f}"
    return (
        f"{result.loser.display_name} 存款多 {penalty.rich_excess}，"
        f"赔付 ×{penalty.multiplier:.1f}"
    )


__all__ = [
    "MAX_ROUNDS",
    "MODE_COIN",
    "MODE_COIN_AND_MUTE",
    "MODE_IMMUNE",
    "SIDE_CHALLENGER",
    "SIDE_TARGET",
    "DuelResult",
    "DuelRoll",
    "DuelRound",
    "DuelService",
    "MuteState",
    "PenaltyPlan",
    "plan_penalty",
    "round_half_up",
    "roll_duel",
    "roll_once",
]
