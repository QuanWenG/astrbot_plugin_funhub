"""指令词表。

放在这里而不是 ``main.py``，是因为唤醒判定也要用它：QQ 官方机器人上 ``self_id``
有时是被 @ 的**对手**，只有当这条消息"看起来像决斗指令"时才能这么认定
（老插件 ``_is_bot_target_id`` 的判据）。词表属于策略，不该只存在于宿主接线文件里。
"""

from __future__ import annotations

#: 指令名与别名。AstrBot 要求唤醒前缀；匹配规则是「完全相等或以『指令名+空格』开头」。
COMMAND_ALIASES: dict[str, set[str]] = {
    "签到": {"打卡", "每日签到"},
    "面板": {"我的信息", "我的面板"},
    "排行": {"排行榜"},
    # 「艾斯比 / 啥比」是老插件的挑战唤起词，作为决斗的便捷别名一起移植。
    "决斗": {"挑战", "艾斯比", "啥比"},
    "轮盘": {"转盘"},
    "开枪": set(),
    "救一下": {"解禁"},
    # 「补一枪」照搬 Pallas-Bot 的 judgment：给中弹被禁言的人追加一次禁言。
    "补一枪": {"补枪"},
}

#: 全部可识别的指令名（主名 + 别名），顺序稳定便于测试。
ALL_COMMAND_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        name
        for command, aliases in COMMAND_ALIASES.items()
        for name in (command, *sorted(aliases))
    ),
)

#: 别名 → 主名，用于把 ``@机器人 艾斯比`` 归一成「决斗」。
COMMAND_CANONICAL: dict[str, str] = {
    name: command
    for command, aliases in COMMAND_ALIASES.items()
    for name in (command, *aliases)
}


def canonical_command(name: str) -> str:
    """把别名归一成主名（未知名字原样返回）。"""
    return COMMAND_CANONICAL.get(name, name)

#: 老插件的挑战唤起词：它们本身就是唤起词，@ 到对手即可开打，不需要唤醒机器人
#: （见老插件 handles/command_handler.py 的 is_alias_challenge_event）。
DUEL_WAKE_WORDS = frozenset({"艾斯比", "啥比"})

#: 能表示"这是决斗"的所有词。
DUEL_WORDS: frozenset[str] = frozenset({"决斗", "挑战"}) | DUEL_WAKE_WORDS

#: 允许"通配"出现在句子任何位置的指令名。
#:
#: 老插件对挑战唤起词用的是 ``any(word in message ...)``，所以「艾斯比吧你」这种
#: 夹在句子里、带后缀的说法同样能触发。这里把整个决斗词族都放开：
#: 「来决斗啊 @某人」「挑战一下 @某人」都算。
DUEL_WILDCARD_NAMES: tuple[str, ...] = tuple(sorted(DUEL_WORDS))
