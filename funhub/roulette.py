"""龟龟轮盘：6 个弹槽 1 颗子弹，谁扣扳机谁可能中弹。

玩法照搬 Pallas-Bot 的 `packages/roulette`（左轮轮盘），把惩罚从"禁言/踢人"换成
"扣龟龟币 + 禁言"，并让参与者有收益：

* **弹槽位置开局就定**（第几枪响是确定的），所以一直开就是赌命；
* **空枪**：第 n 枪 +``safe_reward + (n-1) × safe_reward_step`` 币（越往后越肥）；
* **中弹**：扣 ``hit_penalty`` 币（按"尽所能"，余额不够就全给）+ 禁言
  ``mute_seconds``；扣款里 ``share_percent``%（默认全给）平分给**本局其他开过枪的人**
  —— 中弹的人自己不分、没扣过扳机的人不分，剩下的回收（不增发）；只禁言不踢人；
  不能禁言的角色（群主/管理员）只扣币；
* **炸膛**：**抽到子弹的那一枪**（不管子弹在第几个弹槽）有 ``misfire_percent``%
  （默认 25%）概率炸膛 —— 开枪的人不中弹，反而白拿 ``misfire_reward``（默认 500），
  本局结束；
* 超时 ``timeout_seconds`` 秒没人开枪，本局自动作废。

状态放在内存里（一局就几分钟），按 `平台:群` 隔离；同一个群同时只有一局。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .drunk import DrunkSource, DrunkState
from .platform_api import ADMIN_ROLES, GroupPlatform
from .players import Player, PlayerIdentity, PlayerRepository

#: 开枪结果
SHOT_NO_ROUND = "no_round"
SHOT_MISFIRE = "misfire"
SHOT_MISS = "miss"
SHOT_HIT = "hit"

#: 币的流水来源
REASON_SAFE = "roulette_safe"
REASON_MISFIRE = "roulette_misfire"
REASON_HIT = "roulette_hit"
REASON_SHARE = "roulette_share"

#: `/救一下` 的失败（救援本身的失败，与开枪无关）
RESCUE_MISFIRE = "misfire"

#: 两个会吃冷却的操作名（按人 + 按操作各自计时）
ACTION_JUDGMENT = "judgment"
ACTION_RESCUE = "rescue"


@dataclass(slots=True)
class RouletteRound:
    """一局轮盘的内存状态。"""

    key: str
    chamber: int
    shots: int = 0
    #: 本局扣过扳机的人（发起者没开枪就不算），中弹的扣款平分给他们
    shooters: list[str] = field(default_factory=list)
    started_at: float = 0.0
    touched_at: float = 0.0
    finished: bool = False

    @property
    def remaining(self) -> int:
        """还要几枪才会响。"""
        return self.chamber - self.shots

    def fired(self, user_id: str) -> None:
        """记一次"开过枪"。"""
        if user_id not in self.shooters:
            self.shooters.append(user_id)


@dataclass(frozen=True, slots=True)
class StartOutcome:
    started: bool
    busy: bool
    chambers: int
    safe_reward: int
    safe_reward_step: int
    hit_penalty: int
    misfire_percent: float
    misfire_reward: int
    mute_seconds: int
    share_percent: int


@dataclass(frozen=True, slots=True)
class VictimResult:
    """一个中弹的人这次的实际结果（醉酒多杀时会有好几个）。"""

    user_id: str
    name: str
    penalty: int
    paid: int
    muted: bool = False
    mute_blocked: bool = False


@dataclass(frozen=True, slots=True)
class _PunishStep:
    """内部用：扣完钱之后的一次结算。"""

    identity: PlayerIdentity
    name: str
    paid: int
    player: Player


@dataclass(frozen=True, slots=True)
class ShotOutcome:
    kind: str
    shots: int = 0
    chambers: int = 0
    reward: int = 0
    penalty: int = 0
    paid: int = 0
    shared: int = 0
    sharers: int = 0
    share_names: tuple[str, ...] = ()
    """分到钱的人（按入账顺序），供卡片直接写名字。"""
    victims: tuple[VictimResult, ...] = ()
    """中弹的人（第一个是扣扳机的那个，之后是醉酒被拉来陪罚的）。"""
    drunk: bool = False
    """这一枪是不是醉酒多杀。"""
    muted: bool = False
    mute_seconds: int = 0
    mute_blocked: bool = False
    coin_short: bool = False
    player: Player | None = None

    @property
    def hit(self) -> bool:
        return self.kind == SHOT_HIT

    @property
    def share_each(self) -> int:
        return self.shared // self.sharers if self.sharers else 0


@dataclass(frozen=True, slots=True)
class RescueOutcome:
    kind: str  # no_round | misfire | cooldown | rescued | protected | failed
    target: str = ""
    target_name: str = ""
    seconds: int = 0
    """被"补一枪冷却"挡住时，还要等多少秒。"""


@dataclass(frozen=True, slots=True)
class Punished:
    """中弹被禁言的人（``/补一枪`` 的合法目标）。"""

    user_id: str
    name: str
    until: float
    """禁言到期时刻（``clock`` 的口径），过期就自动从名单里消失。"""


@dataclass(frozen=True, slots=True)
class JudgmentOutcome:
    """``/补一枪`` 的结果。"""

    kind: str  # no_one | not_punished | cooldown | fail | backfire | protected | punished
    names: tuple[str, ...] = ()
    seconds: int = 0
    actor_name: str = ""


class RouletteService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        drunk: DrunkSource | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.rng = rng
        self.clock = clock
        #: 醉酒状态来源（别的插件；读不到就是清醒）
        self.drunk = drunk or DrunkSource()
        self.players = PlayerRepository(database)
        self._rounds: dict[str, RouletteRound] = {}
        #: 中弹被禁言的人，按 ``平台:群`` 分组 —— ``/补一枪`` 只能打他们
        self._punished: dict[str, dict[str, Punished]] = {}
        #: 每个人最后一次"补一枪 / 救一下"的尝试时刻（按人 + 按操作，防连刷）
        self._cooldowns: dict[str, dict[tuple[str, str], float]] = {}
        #: 一局之内的开枪必须串行，否则两个人同时扣扳机会算错弹槽
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def key_for(identity: PlayerIdentity) -> str:
        return f"{identity.platform}:{identity.group_id}"

    def active_round(self, identity: PlayerIdentity) -> RouletteRound | None:
        """当前进行中的一局（已超时/已结束会顺手清掉）。"""
        key = self.key_for(identity)
        current = self._rounds.get(key)
        if current is None:
            return None
        if current.finished or self._expired(current):
            self._rounds.pop(key, None)
            return None
        return current

    def _expired(self, current: RouletteRound) -> bool:
        timeout = self.settings.roulette.timeout_seconds
        return self.clock() - current.touched_at > timeout

    def _random(self) -> random.Random:
        return self.rng or random

    # ---------------------------------------------------------------- 开局

    async def start(self, identity: PlayerIdentity) -> StartOutcome:
        """开一局。同一群同时只能有一局。"""
        settings = self.settings.roulette
        async with self._lock:
            if self.active_round(identity) is not None:
                return self._start_outcome(settings, started=False, busy=True)
            now = self.clock()
            chambers = max(int(settings.chambers), 1)
            current = RouletteRound(
                key=self.key_for(identity),
                chamber=self._random().randint(1, chambers),
                started_at=now,
                touched_at=now,
            )
            self._rounds[current.key] = current
        return self._start_outcome(settings, started=True, busy=False)

    @staticmethod
    def _start_outcome(settings, *, started: bool, busy: bool) -> StartOutcome:
        return StartOutcome(
            started=started,
            busy=busy,
            chambers=max(int(settings.chambers), 1),
            safe_reward=max(int(settings.safe_reward), 0),
            safe_reward_step=max(int(settings.safe_reward_step), 0),
            hit_penalty=max(int(settings.hit_penalty), 0),
            misfire_percent=float(settings.misfire_percent),
            misfire_reward=max(int(settings.misfire_reward), 0),
            mute_seconds=max(int(settings.mute_seconds), 0),
            share_percent=max(min(int(settings.share_percent), 100), 0),
        )

    # ---------------------------------------------------------------- 开枪

    async def shoot(
        self,
        identity: PlayerIdentity,
        *,
        platform: GroupPlatform,
    ) -> ShotOutcome:
        """扣一次扳机。"""
        settings = self.settings.roulette
        async with self._lock:
            current = self.active_round(identity)
            if current is None:
                return ShotOutcome(kind=SHOT_NO_ROUND)

            current.shots += 1
            current.touched_at = self.clock()
            current.fired(identity.user_id)
            shots = current.shots
            chambers = max(int(settings.chambers), 1)

            if shots < current.chamber:
                reward = self._safe_reward(settings, shots)
                player = await self._grant(identity, reward, REASON_SAFE, ref=f"shot{shots}")
                return ShotOutcome(
                    kind=SHOT_MISS,
                    shots=shots,
                    chambers=chambers,
                    reward=reward,
                    player=player,
                )

            # 抽到子弹的这一枪（子弹在第几个弹槽就第几枪）：先掷炸膛，没炸才中弹
            if self._chance(settings.misfire_percent):
                reward = max(int(settings.misfire_reward), 0)
                player = await self._grant(identity, reward, REASON_MISFIRE, ref=f"shot{shots}")
                current.finished = True
                self._rounds.pop(current.key, None)
                return ShotOutcome(
                    kind=SHOT_MISFIRE,
                    shots=shots,
                    chambers=chambers,
                    reward=reward,
                    player=player,
                )

            # 中弹：本局结束
            current.finished = True
            others = [user_id for user_id in current.shooters if user_id != identity.user_id]
            self._rounds.pop(current.key, None)

        return await self._punish(
            identity,
            others=others,
            platform=platform,
            shots=shots,
            chambers=chambers,
        )

    @staticmethod
    def _safe_reward(settings, shots: int) -> int:
        """第 ``shots`` 枪的空枪奖励：第一枪 ``safe_reward``，之后每枪再加一档。"""
        base = max(int(settings.safe_reward), 0)
        step = max(int(settings.safe_reward_step), 0)
        return base + max(int(shots) - 1, 0) * step

    async def _punish(
        self,
        identity: PlayerIdentity,
        *,
        others: list[str],
        platform: GroupPlatform,
        shots: int,
        chambers: int,
    ) -> ShotOutcome:
        settings = self.settings.roulette
        penalty = max(int(settings.hit_penalty), 0)
        # 醉汉打牌：中弹的人还会拉几个同局开过枪的一起挨罚（原版"醉酒随机命中多人"）
        extras = await self._drunk_extras(identity, others)
        punished_ids = {identity.user_id, *extras}
        # 分账只给"开过枪但没中弹"的人：同案犯之间不互相退款
        sharers_ids = [user_id for user_id in others if user_id not in punished_ids]
        targets = [identity, *(self._same_group(identity, user_id) for user_id in extras)]

        results: list[_PunishStep] = []
        async with self.database.transaction() as connection:
            for target in targets:
                results.append(
                    await self._charge_in_db(connection, target, penalty=penalty),
                )

            total_paid = sum(step.paid for step in results)
            share_pool = total_paid * max(min(int(settings.share_percent), 100), 0) // 100
            sharers = 0
            shared = 0
            share_names: list[str] = []
            if share_pool > 0 and sharers_ids:
                each = share_pool // len(sharers_ids)
                if each > 0:
                    for user_id in sharers_ids:
                        mate, _ = await self.players.get_or_create_in_db(
                            connection,
                            self._same_group(identity, user_id),
                        )
                        await self.players.apply_coin_delta_in_db(
                            connection,
                            player_pk=mate.id,
                            delta=each,
                            reason=REASON_SHARE,
                            ref=identity.user_id,
                        )
                        shared += each
                        sharers += 1
                        share_names.append(mate.display_name)

        muted_any = False
        mute_seconds = 0
        mute_blocked_any = False
        victims: list[VictimResult] = []
        for step in results:
            muted = False
            blocked = False
            if settings.mute_seconds > 0:
                muted, seconds, blocked = await self._try_mute(step.identity, platform=platform)
                mute_seconds = seconds or mute_seconds
                if muted:
                    muted_any = True
                    # 记进"中弹被禁言"名单：/补一枪 只能打这些人
                    self._remember_punished(
                        self.key_for(identity),
                        step.identity.user_id,
                        step.name,
                        seconds,
                    )
                elif blocked:
                    mute_blocked_any = True
            victims.append(
                VictimResult(
                    user_id=step.identity.user_id,
                    name=step.name,
                    penalty=penalty,
                    paid=step.paid,
                    muted=muted,
                    mute_blocked=blocked,
                ),
            )

        primary = victims[0]
        return ShotOutcome(
            kind=SHOT_HIT,
            shots=shots,
            chambers=chambers,
            penalty=penalty,
            paid=primary.paid,
            shared=shared,
            sharers=sharers,
            share_names=tuple(share_names),
            muted=primary.muted,
            mute_seconds=mute_seconds,
            mute_blocked=primary.mute_blocked,
            coin_short=primary.paid < penalty,
            player=results[0].player,
            victims=tuple(victims),
            drunk=bool(extras),
        )

    @staticmethod
    def _same_group(identity: PlayerIdentity, user_id: str) -> PlayerIdentity:
        return PlayerIdentity(
            platform=identity.platform,
            group_id=identity.group_id,
            user_id=user_id,
            nickname="",
        )

    async def _charge_in_db(
        self,
        connection,
        identity: PlayerIdentity,
        *,
        penalty: int,
    ) -> _PunishStep:
        """扣一次中弹赔付（按"尽所能"），返回这一步的结果。"""
        player, _ = await self.players.get_or_create_in_db(connection, identity)
        before = player.coins
        if penalty:
            await self.players.apply_coin_delta_in_db(
                connection,
                player_pk=player.id,
                delta=-penalty,
                reason=REASON_HIT,
                ref=f"roulette:{identity.user_id}",
            )
        after = await self.players.reload_in_db(connection, player.id)
        return _PunishStep(
            identity=PlayerIdentity(
                platform=identity.platform,
                group_id=identity.group_id,
                user_id=identity.user_id,
                nickname=after.display_name,
            ),
            name=after.display_name,
            paid=before - after.coins,
            player=after,
        )

    async def _drunk_extras(self, identity: PlayerIdentity, others: list[str]) -> list[str]:
        """醉酒时额外拉几个同局开过枪的人陪罚（清醒则一个都不拉）。

        醉是前提，**走火本身还有 ``drunk_extra_percent``% 的概率**（默认 25%），
        走火之后拉几个是随机的 1 ~ ``drunk_extra_max``。
        """
        settings = self.settings.roulette
        limit = max(int(settings.drunk_extra_max), 0)
        if limit <= 0 or not others:
            return []
        state = await self.drunk_state(identity)
        if not state.drunk:
            return []
        if not self._chance(settings.drunk_extra_percent):
            return []
        pool = list(others)
        count = self._random().randint(1, min(len(pool), limit))
        self._random().shuffle(pool)
        return pool[:count]

    async def _try_mute(
        self,
        identity: PlayerIdentity,
        *,
        platform: GroupPlatform,
    ) -> tuple[bool, int, bool]:
        """返回 ``(是否已禁言, 禁言秒数, 是否因为对方是管理员而没禁)``。

        群主/管理员禁不了，只扣币（与决斗同一套口径）。
        """
        seconds = int(self.settings.roulette.mute_seconds)
        role = await platform.member_role(identity.user_id)
        if role in ADMIN_ROLES:
            return False, seconds, True
        outcome = await platform.mute(identity.user_id, seconds)
        return outcome.ok, seconds if outcome.ok else 0, False

    async def _grant(
        self,
        identity: PlayerIdentity,
        amount: int,
        reason: str,
        *,
        ref: str = "",
    ) -> Player:
        async with self.database.transaction() as connection:
            player, _ = await self.players.get_or_create_in_db(connection, identity)
            await self.players.apply_coin_delta_in_db(
                connection,
                player_pk=player.id,
                delta=amount,
                reason=reason,
                ref=ref,
            )
            return await self.players.reload_in_db(connection, player.id)

    # ---------------------------------------------------------------- 救一下

    async def rescue(
        self,
        actor: PlayerIdentity,
        target_id: str,
        *,
        platform: GroupPlatform,
    ) -> RescueOutcome:
        """把中弹被禁言的人捞出来（``duration=0`` 即解禁）；有概率失败。

        中弹会立刻结束本局，所以救援不需要"局内"状态 —— 谁被禁言了都能捞。
        **同一个人不能连着救**：``action_cooldown_seconds`` 秒内不能再救一次，
        炸膛 / 接口失败一样要等；但**多个人可以轮流救同一个人**，补枪也不受影响。
        """
        settings = self.settings.roulette
        key = self.key_for(actor)

        wait = self.action_cooldown(key, actor.user_id, action=ACTION_RESCUE)
        if wait:
            return RescueOutcome(kind="cooldown", target=target_id, seconds=wait)

        # 到这儿就算"真救了一次"，成功失败都要吃冷却
        self._note_attempt(key, actor.user_id, ACTION_RESCUE)

        if self._chance(settings.rescue_fail_percent):
            return RescueOutcome(kind=RESCUE_MISFIRE, target=target_id)
        outcome = await platform.mute(target_id, 0)
        if outcome.ok:
            # 捞出来了就不再是"中弹被禁言的人"，补一枪也打不到他
            self.forget_punished(key, target_id)
        return RescueOutcome(
            kind="rescued" if outcome.ok else "failed",
            target=target_id,
        )

    # ---------------------------------------------------------------- 补一枪

    def punished(self, identity: PlayerIdentity) -> list[Punished]:
        """本群还在禁言里的中弹者（过期的顺手清掉）。"""
        return list(self._live_punished(self.key_for(identity)).values())

    def forget_punished(self, key: str, user_id: str) -> None:
        """把人从中弹名单里去掉（禁言结束 / 被人捞走）。"""
        records = self._punished.get(key)
        if not records:
            return
        records.pop(user_id, None)
        if not records:
            self._punished.pop(key, None)

    def _live_punished(self, key: str) -> dict[str, Punished]:
        records = self._punished.get(key)
        if not records:
            return {}
        now = self.clock()
        for user_id, record in list(records.items()):
            if record.until <= now:
                records.pop(user_id, None)
        if not records:
            self._punished.pop(key, None)
            return {}
        return records

    def _remember_punished(self, key: str, user_id: str, name: str, seconds: int) -> None:
        if seconds <= 0:
            return
        records = self._punished.setdefault(key, {})
        previous = records.get(user_id)
        base = max(previous.until, self.clock()) if previous else self.clock()
        records[user_id] = Punished(
            user_id=user_id,
            name=name or (previous.name if previous else user_id),
            until=base + seconds,
        )

    # ------------------------------------------- 补一枪 / 救一下 的冷却

    def _note_attempt(self, key: str, user_id: str, action: str) -> None:
        """记下"这个人刚试过一次这个操作"（成功失败都算）。"""
        self._cooldowns.setdefault(key, {})[(user_id, action)] = self.clock()

    def action_cooldown(self, key: str, user_id: str, *, action: str) -> int:
        """同一个人还要等多少秒才能再做同一个操作（0 = 可以做）。

        「不能连续补枪、也不能连续救」：**按人 + 按操作**各自计时，成功失败都算
        （卡壳、炸膛、救援失败一样要等），免得一直刷到成功。救人 / 补枪之间
        不互斥，别人也不受影响 —— 多人轮流救同一个人是可以的。
        """
        seconds = max(int(self.settings.roulette.action_cooldown_seconds), 0)
        if seconds <= 0:
            return 0
        records = self._cooldowns.get(key)
        if not records:
            return 0
        at = records.get((user_id, action))
        if at is None:
            return 0
        remaining = int(seconds - (self.clock() - at))
        if remaining <= 0:
            records.pop((user_id, action), None)
            if not records:
                self._cooldowns.pop(key, None)
            return 0
        return remaining

    async def judgment(
        self,
        actor: PlayerIdentity,
        targets: Sequence[tuple[str, str]],
        *,
        platform: GroupPlatform,
    ) -> JudgmentOutcome:
        """``/补一枪``：给中弹被禁言的人追加一次禁言。

        照搬 Pallas-Bot ``packages/roulette`` 的 judgment，去掉踢人模式：

        * 只能补**本局中弹、且还在禁言里**的人（``targets`` 为空则对所有这样的人补）；
        * ``judgment_fail_percent``% 卡壳：什么都不发生；
        * ``judgment_backfire_percent``% 反噬：这一枪补到发起者自己头上，而且
          **原版只在牛牛醉酒时才可能翻车** —— 这里默认沿用（醉酒状态从喝酒插件读，
          见 :mod:`funhub.drunk`）；把 ``judgment_backfire_drunk_only`` 关掉则常驻；
        * 群主 / 管理员禁不了，跳过（不踢人）；
        * 成功则追加 ``judgment_seconds`` 秒禁言（在原有禁言上叠加）；
        * **同一个人不能连着补枪**：``action_cooldown_seconds`` 秒内不能再补一次，
          卡壳 / 反噬这些失败的尝试一样要等（见 :meth:`action_cooldown`）。
        """
        settings = self.settings.roulette
        key = self.key_for(actor)
        actor_name = actor.nickname or actor.user_id

        wait = self.action_cooldown(key, actor.user_id, action=ACTION_JUDGMENT)
        if wait:
            return JudgmentOutcome(kind="cooldown", seconds=wait, actor_name=actor_name)

        live = self._live_punished(key)
        if not live:
            return JudgmentOutcome(kind="no_one")

        chosen: list[tuple[str, str]] = []
        if targets:
            chosen = [
                (user_id, name or live[user_id].name)
                for user_id, name in targets
                if user_id in live
            ]
            if not chosen:
                # 原版：补一枪只能对本局被禁言的成员
                return JudgmentOutcome(kind="not_punished", actor_name=actor_name)
        else:
            chosen = [(record.user_id, record.name) for record in live.values()]

        # 到这儿才算"真补了一次"，后面的成功失败都要吃冷却
        self._note_attempt(key, actor.user_id, ACTION_JUDGMENT)

        if self._chance(settings.judgment_fail_percent):
            return JudgmentOutcome(kind="fail", actor_name=actor_name)

        if await self.may_backfire(actor):
            ok, seconds, _blocked = await self._apply_judgment(
                actor.user_id,
                seconds=settings.judgment_seconds,
                platform=platform,
            )
            if not ok:
                return JudgmentOutcome(kind="protected", actor_name=actor_name)
            self._remember_punished(key, actor.user_id, actor_name, seconds)
            return JudgmentOutcome(
                kind="backfire",
                names=(actor_name,),
                seconds=seconds,
                actor_name=actor_name,
            )

        succeeded: list[str] = []
        seconds = 0
        for user_id, name in chosen:
            ok, applied, _blocked = await self._apply_judgment(
                user_id,
                seconds=settings.judgment_seconds,
                platform=platform,
            )
            if not ok:
                continue
            self._remember_punished(key, user_id, name, applied)
            succeeded.append(name or user_id)
            seconds = applied

        if not succeeded:
            return JudgmentOutcome(kind="protected", actor_name=actor_name)
        return JudgmentOutcome(
            kind="punished",
            names=tuple(succeeded),
            seconds=seconds,
            actor_name=actor_name,
        )

    async def may_backfire(self, identity: PlayerIdentity) -> bool:
        """这一枪会不会反噬到发起者头上。

        原版 ``self_punish_requires_drunk=True``：只有喝醉了才可能翻车。醉酒状态
        从喝酒插件读（:mod:`funhub.drunk`），读不到就当清醒；把
        ``judgment_backfire_drunk_only`` 关掉则不再看醉酒，随时可能翻车。
        """
        settings = self.settings.roulette
        if settings.judgment_backfire_percent <= 0:
            return False
        if settings.judgment_backfire_drunk_only:
            state = await self.drunk_state(identity)
            if not state.drunk:
                return False
        return self._chance(settings.judgment_backfire_percent)

    async def drunk_state(self, identity: PlayerIdentity) -> DrunkState:
        """这个群现在醉着吗（读不到就是清醒）。任何异常都不该影响打牌。"""
        try:
            return await self.drunk.state(identity)
        except Exception:  # pragma: no cover - 别人的插件出问题不能拖垮轮盘
            return DrunkState()

    async def _apply_judgment(
        self,
        user_id: str,
        *,
        seconds: int,
        platform: GroupPlatform,
    ) -> tuple[bool, int, bool]:
        """追加一次禁言。返回 ``(是否成功, 实际秒数, 是否因为是管理员而没禁)``。"""
        if seconds <= 0:
            return False, 0, False
        role = await platform.member_role(user_id)
        if role in ADMIN_ROLES:
            return False, seconds, True
        outcome = await platform.mute(user_id, seconds)
        return outcome.ok, seconds if outcome.ok else 0, False

    # ---------------------------------------------------------------- 内部

    def _chance(self, percent: float) -> bool:
        if percent <= 0:
            return False
        if percent >= 100:
            return True
        return self._random().random() * 100 < float(percent)
