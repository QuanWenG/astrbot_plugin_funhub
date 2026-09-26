"""全部玩家可见文案的唯一出口。

风格约束（README 同步维护，`tests/test_messages.py` 会把它变成断言）：

* 不用任何 emoji；
* 卡片类回复不超过 3 行，签到卡固定 2 行（命中周期时每命中一个周期追加一行）；
* 语气词克制，整条最多一处，不用波浪号堆叠；
* 不写解释、鼓励、复述性的句子，只给数字和必要的标签；
* 数据行统一 ``标签 值``，分隔符 `` · ``，增量前缀 ``+``，没有的行整行省略。

排行是表格而非卡片，行数由 ``ui.ranking_size`` 决定。文案函数只接受数字与
用户名，方便测试断言。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from .coins import CycleHit

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免运行期循环导入
    from .legacy import ImportReport

#: 常量文案
CHECKIN_FAILED = "签到失败，详情见日志"
DUEL_FAILED = "决斗失败，详情见日志"
ROULETTE_FAILED = "轮盘卡壳了，详情见日志"
EMPTY_RANKING = "本群还没有人签到过"
RANKING_TITLE = "本群排行"
RANKING_TITLE_BY_COINS = "本群排行（按龟龟币）"
NO_ROULETTE = "现在没有进行中的轮盘"
ROULETTE_BUSY = "本群已经有一局轮盘了"
ROULETTE_USAGE = "用法：/轮盘 开局、/开枪 扣扳机、/救一下 @某人"

#: 指令打错时的补一句；决斗多提醒一句要 @ 人，否则最容易"没反应"。
COMMAND_HINT_SUFFIX = {"决斗": "，记得 @ 上对手"}


def command_hint(command: str) -> str:
    """指令名写得不对时给的提示（别让"打错字"看起来像"插件死了"）。"""
    return f"指令是 /{command}{COMMAND_HINT_SUFFIX.get(command, '')}"


def duration_text(seconds: int) -> str:
    """把秒数写成人话：整分钟用「N 分钟」，否则「N 秒」。"""
    seconds = int(seconds)
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


#: 分账最多列几个名字，多的收成「等 N 人」——别让一行名字比卡片还长
SHARE_NAMES_MAX = 4


def percent_text(value: float) -> str:
    """概率写成人话：``一半`` / ``25%``。"""
    number = float(value)
    return "一半" if number == 50 else f"{number:g}%"


def chance_clause(percent: float, *, subject: str = "") -> str:
    """``一半概率`` / ``12.5% 概率`` —— 数字前后该不该有空格只在这里决定。"""
    text = percent_text(percent)
    if text.endswith("%"):
        return f"{subject} {text} 概率".strip()
    return f"{subject}{text}概率"


def name_list(names: Sequence[str], *, limit: int = SHARE_NAMES_MAX) -> str:
    """名字串成一行：``甲、乙``；超过 ``limit`` 个收成 ``甲、乙 等 5 人``。"""
    items = [str(name) for name in names if str(name)]
    if len(items) <= limit:
        return "、".join(items)
    return f"{'、'.join(items[:limit])} 等 {len(items)} 人"


def roulette_start_card(
    *,
    chambers: int,
    safe_reward: int,
    hit_penalty: int,
    mute_seconds: int,
    share_percent: int = 100,
    safe_reward_step: int = 0,
    misfire_percent: float = 0,
    misfire_reward: int = 0,
) -> str:
    """开局卡：几个数字 + 钱的去向。"""
    lines = [f"轮盘装填完毕（{int(chambers)} 个弹槽 · 1 颗子弹）"]

    reward = f"空枪 +{int(safe_reward)} 币"
    if safe_reward_step > 0:
        reward += f"，之后每枪再多 {int(safe_reward_step)}"
    lines.append(reward)

    if misfire_percent > 0:
        boom = f"{chance_clause(misfire_percent, subject='子弹那一枪')}炸膛"
        if misfire_reward > 0:
            boom += f"，开枪的人白拿 {int(misfire_reward)} 币"
        lines.append(boom)

    cost = f"中弹扣 {int(hit_penalty)} 币"
    share = max(min(int(share_percent), 100), 0)
    if hit_penalty > 0 and share > 0:
        where = "全赔" if share >= 100 else f"其中 {share}% 赔"
        cost += f"，{where}给开过枪的人"
    if mute_seconds:
        cost += f"，禁言 {duration_text(mute_seconds)}"
    lines.append(cost)

    lines.append("发 /开枪 扣扳机")
    return "\n".join(lines)


def roulette_miss_card(*, shots: int, chambers: int, reward: int) -> str:
    """空枪：发钱 + 报进度。"""
    return f"空枪（{int(shots)} / {int(chambers)}），+{int(reward)} 币"


def roulette_misfire_card(*, name: str, shots: int, chambers: int, reward: int) -> str:
    """炸膛：开枪的人没中弹，白拿一笔，本局结束。"""
    head = f"炸膛（{int(shots)} / {int(chambers)}）"
    if reward > 0:
        return f"{head}，{name} +{int(reward)} 币"
    return f"{head}，本局结束"


def roulette_drunk_card(
    *,
    victims: Sequence[tuple[str, int]],
    shots: int,
    chambers: int,
    penalty: int,
    mute_seconds: int,
    any_muted: bool,
    any_blocked: bool,
    share_each: int = 0,
    share_names: Sequence[str] = (),
) -> str:
    """醉酒多杀：一行说清谁中弹、一共扣了多少。"""
    names = [name for name, _ in victims]
    total = sum(paid for _, paid in victims)
    lines = [f"醉酒走火，{name_list(names)} 一起中弹（{int(shots)} / {int(chambers)}）"]

    if total <= 0:
        cost = "没币可扣"
    elif all(paid >= penalty for _, paid in victims):
        cost = f"各扣 {int(penalty)} 币（共 {total}）"
    else:
        cost = f"共扣 {total} 币（按余额）"
    if any_muted and mute_seconds:
        cost += f"，禁言 {duration_text(mute_seconds)}"
    elif any_blocked:
        cost += "（管理员禁不了言）"
    lines.append(cost)

    if share_each > 0:
        who = name_list(share_names)
        verb = "各分到" if len(share_names) > 1 else "分到"
        lines.append(f"{who} {verb} {int(share_each)} 币" if who else f"分掉 {int(share_each)} 币")
    return "\n".join(lines)


def roulette_hit_card(
    *,
    name: str,
    shots: int,
    chambers: int,
    paid: int,
    penalty: int,
    muted: bool,
    mute_seconds: int,
    mute_blocked: bool,
    share_each: int,
    sharers: int,
    share_names: Sequence[str] = (),
) -> str:
    """中弹卡：一行结果、一行惩罚、一行分账（写名字，不写"其他 N 人"）。"""
    lines = [f"砰，{name} 中弹（{int(shots)} / {int(chambers)}）"]
    cost = f"扣 {int(paid)} 币" if paid < penalty else f"扣 {int(penalty)} 币"
    if paid < penalty:
        cost += "（余额只有这么多）"
    if muted:
        lines.append(f"{cost}，禁言 {duration_text(mute_seconds)}")
    elif mute_blocked:
        lines.append(f"{cost}（管理员禁不了言）")
    else:
        lines.append(cost)
    if share_each > 0:
        who = name_list(share_names) or f"{int(sharers)} 人"
        verb = "各分到" if sharers > 1 else "分到"
        lines.append(f"{who} {verb} {int(share_each)} 币")
    return "\n".join(lines)


def roulette_rescue_card(*, kind: str, name: str, seconds: int = 0) -> str:
    if kind == "cooldown":
        return f"刚救过了，再等 {duration_text(seconds)}"
    if kind == "misfire":
        return "炸膛了，这一下没救成"
    if kind == "rescued":
        return f"{name} 被捞出来了"
    return f"{name} 没能捞出来，详情见日志"


def roulette_judgment_card(
    *,
    kind: str,
    names: Sequence[str] = (),
    seconds: int = 0,
    actor_name: str = "",
) -> str:
    """``/补一枪`` 的结果卡：一行说清补到谁头上、又禁了多久。"""
    if kind == "no_one":
        return "没有可以补枪的人（名单里的人都出来了）"
    if kind == "not_punished":
        return "只能补本局中弹被禁言的人"
    if kind == "cooldown":
        return (
            f"刚补过了，再等 {duration_text(seconds)}"
            if seconds
            else "刚补过了，先歇会儿"
        )
    if kind == "fail":
        return "卡壳了，这一枪没补上"
    duration = duration_text(seconds) if seconds else ""
    if kind == "backfire":
        who = actor_name or (names[0] if names else "")
        return f"枪口歪了，{who} 自己又被禁言 {duration}"
    if kind == "protected":
        return "补不动，对方是管理员（禁不了言）"
    who = name_list(names) or "对方"
    verb = "各又被禁言" if len(names) > 1 else "又被禁言"
    return f"补了一枪，{who} {verb} {duration}"


def checkin_card(
    *,
    total: int,
    streak_days: int,
    coins_total: int,
    cycle_hits: Sequence[CycleHit] = (),
) -> str:
    """签到成功卡片：两行 + 每个命中周期一行。"""
    lines = [
        f"签到成功，+{int(total)} 币",
        f"连续 {int(streak_days)} 天 · 龟龟币 {int(coins_total)}",
    ]
    lines.extend(cycle_line(hit) for hit in cycle_hits)
    return "\n".join(lines)


def checkin_already_card(
    *,
    coins_gain: int,
    streak_days: int,
    coins_total: int,
) -> str:
    """当天重复签到：复用当日已发放的数值，不再发币。"""
    return "\n".join(
        [
            "今天已经签过啦",
            f"+{int(coins_gain)} 币 · 连续 {int(streak_days)} 天 · 龟龟币 {int(coins_total)}",
        ],
    )


def cycle_line(hit: CycleHit) -> str:
    """周期奖励行，例如「连签 7 天，额外 +300 币」。"""
    return f"连签 {int(hit.days)} 天，额外 +{int(hit.bonus)} 币"


def auto_checkin_cycles(*, streak_days: int, cycle_hits: Sequence[CycleHit]) -> str:
    """自动签到只在命中周期时出声。"""
    lines = [f"连续签到 {int(streak_days)} 天"]
    lines.extend(cycle_line(hit) for hit in cycle_hits)
    return "\n".join(lines)


def profile_card(
    *,
    level: int,
    streak_days: int,
    total_checkins: int,
    coins: int,
    rank: int,
    best_streak: int,
    cycle_days: int,
    days_to_cycle: int,
) -> str:
    """面板：三行，覆盖等级 / 连签 / 累计 / 币 / 排名 / 最长连签 / 距周期。"""
    return "\n".join(
        [
            f"等级 Lv.{int(level)} · 连续签到 {int(streak_days)} 天 · 累计 {int(total_checkins)} 天",
            f"龟龟币 {int(coins)} · 本群第 {int(rank)}",
            f"最长连签 {int(best_streak)} 天 · 距 {int(cycle_days)} 天周期还差 {int(days_to_cycle)} 天",
        ],
    )


def ranking_card(entries: Sequence[tuple[int, str, int, int]], *, by_coins: bool) -> str:
    """排行表格。``entries`` 为 ``(名次, 昵称, 等级, 龟龟币)``，币是当前存款。"""
    if not entries:
        return EMPTY_RANKING
    title = RANKING_TITLE_BY_COINS if by_coins else RANKING_TITLE
    lines = [title]
    lines.extend(
        f"{int(rank)}. {name} Lv.{int(level)} · {int(coins)} 币"
        for rank, name, level, coins in entries
    )
    return "\n".join(lines)


def rank_card(*, name: str, rank: int, level: int, coins: int) -> str:
    """查某个群成员的名次。"""
    return "\n".join(
        [
            f"{name} 本群第 {int(rank)}",
            f"Lv.{int(level)} · 龟龟币 {int(coins)}",
        ],
    )


def no_record(name: str) -> str:
    return f"{name} 暂无记录"


def duel_card(
    *,
    challenger: str,
    target: str,
    rolls_text: str,
    mode: str,
    winner: str,
    loser: str,
    gain: int,
    penalty_note: str = "",
    mute_failure: str = "",
    immune_reason: str = "",
    duration_text: str = "",
) -> str:
    """决斗卡：标题 / 点数 / 结果，最多再跟两行奖惩。

    ``rolls_text`` 是已经排好的点数（加赛时形如 ``50 : 50 → 90 : 10``）；
    ``mode`` 取值见 ``funhub.duel``：``coin`` / ``coin_and_mute`` / ``immune``；
    ``penalty_note`` 是倍率说明（如「龟乙 存款多 2000，赔付 ×1.5」），没有就不占行。
    """
    del mute_failure  # 失败原因只进日志；卡片只讲赔了多少
    lines = [
        f"决斗 · {challenger} vs {target}",
        rolls_text,
    ]
    if mode == "immune":
        del immune_reason  # 目前只剩"正在禁言中"这一种豁免
        lines.append(f"{winner} 胜，{loser} 正在禁言中，本次不罚")
        return "\n".join(lines)

    lines.append(f"{winner} 胜，赢得 {int(gain)} 龟龟币")
    if mode == "coin_and_mute":
        lines.append(f"{loser} 被禁言 {duration_text}")
    if penalty_note:
        lines.append(penalty_note)
    return "\n".join(lines)


def import_summary(report: ImportReport, *, preview: bool | None = None) -> str:
    """CLI 用的一行导入回执。"""
    is_preview = report.dry_run if preview is None else preview
    parts = [
        f"新玩家 {report.players_new}",
        f"更新 {report.players_updated}",
        f"新增签到 {report.checkins_inserted}",
        f"补发 {report.coins_granted} 币",
    ]
    if report.checkins_skipped:
        parts.append(f"跳过 {report.checkins_skipped}")
    if report.errors:
        parts.append(f"异常 {len(report.errors)}")
    head = "预览：" if is_preview else "导入完成："
    return head + " · ".join(parts)
