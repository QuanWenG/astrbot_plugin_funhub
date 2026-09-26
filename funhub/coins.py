"""龟龟币奖励公式。

这里是整个插件唯一的"数值政策"，实时签到与历史数据补发共用同一份实现，
所以补发总额天然等于"这些记录当年按今天的规则签到会拿到多少"。

口径（README 有同一张表）::

    单次所得 = 基础奖
             + 随机奖(0..random_bonus_max)
             + 连签加成 = min(streak-1, cap_days) * per_day
             + 7 天周期奖  (streak 是 7 的倍数时)
             + 30 天周期奖 (streak 是 30 的倍数时，与 7 天周期独立判定、可叠加)
             + 首次签到礼 (仅玩家第一次签到)

100 币 ≈ 1 元只是标定奖励量级用的内部口径，不对玩家展示。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .config import CoinSettings


@dataclass(frozen=True, slots=True)
class CycleHit:
    """命中的一个奖励周期。"""

    days: int
    bonus: int


@dataclass(frozen=True, slots=True)
class CheckinReward:
    """一次签到的完整明细，便于逐项断言与展示。"""

    base: int
    random_bonus: int
    streak_bonus: int
    cycle_hits: tuple[CycleHit, ...]
    first_bonus: int
    total: int

    @property
    def cycle_bonus(self) -> int:
        return sum(hit.bonus for hit in self.cycle_hits)

    @property
    def hit_cycles(self) -> bool:
        return bool(self.cycle_hits)


def streak_bonus_for(coins: CoinSettings, streak_days: int) -> int:
    """连签加成：每多连签一天加一点，到 ``streak_bonus_cap_days`` 封顶。"""
    steps = max(int(streak_days) - 1, 0)
    if steps > coins.streak_bonus_cap_days:
        steps = coins.streak_bonus_cap_days
    return steps * coins.streak_bonus_per_day


def cycle_hits_for(coins: CoinSettings, streak_days: int) -> tuple[CycleHit, ...]:
    """命中哪些周期的奖励。连签天数持续累加，所以周期会反复命中。"""
    streak = int(streak_days)
    if streak <= 0:
        return ()
    hits = [
        CycleHit(days=days, bonus=bonus)
        for days, bonus in coins.cycles
        if days > 0 and streak % days == 0
    ]
    return tuple(hits)


def roll_random_bonus(coins: CoinSettings, rng: random.Random | None = None) -> int:
    """实时签到用的随机奖。"""
    if coins.random_bonus_max <= 0:
        return 0
    source = rng or random
    return source.randint(0, coins.random_bonus_max)


def expected_random_bonus(coins: CoinSettings) -> int:
    """补发历史记录时使用的随机奖期望值。

    补发必须可复现：同一个导出文件重复导入、或换台机器导入，结果都要一模一样，
    所以这里取均匀分布的期望值而不是重新掷骰子。
    """
    return coins.random_bonus_max // 2


def compute_reward(
    coins: CoinSettings,
    *,
    streak_days: int,
    is_first_checkin: bool,
    random_bonus: int,
) -> CheckinReward:
    """把各分量合成一次签到的所得。"""
    streak = max(int(streak_days), 0)
    base = max(coins.base_reward, 0)
    randomness = max(int(random_bonus), 0)
    bonus = streak_bonus_for(coins, streak)
    hits = cycle_hits_for(coins, streak)
    first = max(coins.first_checkin_bonus, 0) if is_first_checkin else 0
    total = base + randomness + bonus + sum(hit.bonus for hit in hits) + first
    return CheckinReward(
        base=base,
        random_bonus=randomness,
        streak_bonus=bonus,
        cycle_hits=hits,
        first_bonus=first,
        total=total,
    )


def backfill_reward_for_record(coins: CoinSettings, streak_days: int) -> CheckinReward:
    """补发单条历史记录时的应得额（期望随机值、不含首次签到礼）。

    老用户本来就没有"首次签到礼"这项，给他补上会让补发口径变成两套规则，
    所以这里显式排除。
    """
    return compute_reward(
        coins,
        streak_days=streak_days,
        is_first_checkin=False,
        random_bonus=expected_random_bonus(coins),
    )
