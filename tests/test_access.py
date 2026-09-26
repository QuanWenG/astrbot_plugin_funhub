"""白名单匹配、指令识别与事件取值。"""

from __future__ import annotations

import pytest

from funhub.access import (
    allows,
    at_mentions,
    at_targets,
    bot_ids,
    identity_from_event,
    in_whitelist,
    is_woken,
    looks_like_command,
    looks_like_duel,
    message_text,
    near_miss_command,
    parse_command,
    parse_ranking_args,
    strip_mentions,
    text_mentions,
    whitelist_candidates,
    whitelist_only,
)
from funhub.commands import ALL_COMMAND_NAMES
from funhub.config import load_settings

from .conftest import FakeAt, FakeEvent

COMMANDS = ("签到", "打卡", "每日签到", "面板", "排行", "排行榜")


def test_empty_whitelist_denies_everything():
    assert not allows((), platform_id="aiocqhttp", platform_name="x", group_id="100", unified_msg_origin="")


@pytest.mark.parametrize(
    "entry",
    ["100", "aiocqhttp:100", "other:100", "aiocqhttp:GroupMessage:100"],
)
def test_whitelist_entry_forms_are_accepted(entry: str):
    event = FakeEvent(platform_id="aiocqhttp", platform_name="other", group_id="100")
    assert in_whitelist(load_settings({"access": {"group_whitelist": [entry]}}), event)


def test_other_group_is_denied():
    settings = load_settings({"access": {"group_whitelist": ["999"]}})
    assert not in_whitelist(settings, FakeEvent(group_id="100"))


def test_private_chat_is_denied_even_when_listed():
    settings = load_settings({"access": {"group_whitelist": ["200"]}})
    event = FakeEvent(group_id="", unified_msg_origin="aiocqhttp:FriendMessage:200")
    assert not in_whitelist(settings, event)


def test_candidates_include_umo_and_prefixed_group():
    candidates = whitelist_candidates(
        platform_id="aiocqhttp",
        platform_name="napcat",
        group_id="100",
        unified_msg_origin="aiocqhttp:GroupMessage:100",
    )
    assert candidates == {"100", "aiocqhttp:100", "napcat:100", "aiocqhttp:GroupMessage:100"}


@pytest.mark.parametrize(
    ("message", "awake", "expected"),
    [
        ("签到", True, True),
        ("签到", False, False),
        ("签到 额外", True, True),
        ("签到啦", True, False),
        ("排行币", True, False),
        ("排行 币", True, True),
        ("排行榜", True, True),
        ("", True, False),
        ("你好", True, False),
        # QQ 官方机器人会把 @ 标记留在文本里
        ("<@BE7AA4113AC560F0A026F17176580BDE> 签到", True, True),
        ("<@!1234> 排行 币", True, True),
        ("<@1234> 签到啦", True, False),
    ],
)
def test_looks_like_command(message: str, awake: bool, expected: bool):
    event = FakeEvent(message=message, is_at_or_wake_command=awake)
    assert looks_like_command(event, COMMANDS) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("签到", ("签到", "")),
        ("签到 额外", ("签到", "额外")),
        # @ 标记会被清掉，决斗对象由 at_mentions 负责，不走参数
        ("<@1234> 艾斯比 <@5678>", ("艾斯比", "")),
        ("排行 币", ("排行", "币")),
        ("排行榜", ("排行榜", "")),
        ("签到啦", None),
        ("", None),
    ],
)
def test_parse_command(message: str, expected):
    assert parse_command(FakeEvent(message=message), (*COMMANDS, "艾斯比")) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # 老插件的唤起词是"句中出现即算"
        ("艾斯比吧你", ("艾斯比", "吧你")),
        ("你个啥比", ("啥比", "")),
        ("来决斗啊", ("决斗", "啊")),
        ("挑战一下", ("挑战", "一下")),
        ("别艾斯比了", ("艾斯比", "了")),
        # 句中多个时取最早出现的
        ("先啥比后艾斯比", ("啥比", "后艾斯比")),
        # 前缀匹配优先
        ("签到 艾斯比", ("签到", "艾斯比")),
        # 不在通配名单里的仍按前缀匹配
        ("今天天气不错", None),
        ("我想面板一下", None),
    ],
)
def test_parse_command_wildcard_matches_anywhere(message: str, expected):
    names = (*COMMANDS, "艾斯比", "啥比", "决斗", "挑战")
    wildcard = ("决斗", "挑战", "啥比", "艾斯比")
    assert parse_command(FakeEvent(message=message), names, wildcard=wildcard) == expected


def test_parse_command_without_wildcard_keeps_strict_matching():
    event = FakeEvent(message="艾斯比吧你")
    assert parse_command(event, (*COMMANDS, "艾斯比")) is None
    assert parse_command(event, (*COMMANDS, "艾斯比"), wildcard=("艾斯比",)) == (
        "艾斯比",
        "吧你",
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # 插件叫龟龟乐园、币叫龟龟币，玩家顺手就会打成「龟龟轮盘」
        ("龟龟轮盘", ("轮盘", "")),
        ("龟龟排行 币", ("排行", "币")),
        ("龟龟签到", ("签到", "")),
        ("龟龟 签到", ("签到", "")),
        # 只有"装饰 + 指令名"才算，光有装饰不认
        ("龟龟", None),
        ("龟龟乐园", None),
        # 正常写法不受影响
        ("轮盘", ("轮盘", "")),
    ],
)
def test_parse_command_tolerates_the_decoration(message: str, expected):
    assert parse_command(FakeEvent(message=message), ALL_COMMAND_NAMES) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("轮盘啦", "轮盘"),
        ("龟龟轮盘", "轮盘"),
        ("签到啦", "签到"),
        ("艾斯比吧你", "艾斯比"),
        # 指令名不在最前面 —— 那是闲聊，不是打错指令
        ("今天这个面板挺好看", None),
        ("我要玩轮盘", None),
        # 名字后面跟太多字，也不猜
        ("轮盘怎么玩啊教教我", None),
        ("今天天气不错", None),
    ],
)
def test_near_miss_command(message: str, expected: str | None):
    event = FakeEvent(message=message, is_at_or_wake_command=True)
    assert near_miss_command(event, ALL_COMMAND_NAMES) == expected


def test_near_miss_command_needs_a_wake():
    """没叫机器人就不是指令，别自作多情。"""
    event = FakeEvent(message="轮盘啦", is_at_or_wake_command=False)
    assert near_miss_command(event, ALL_COMMAND_NAMES) is None


def test_text_mentions_reads_markup_and_ignores_self():
    event = FakeEvent(
        message="<@1234> 决斗 <@5678> <@!9012> <@all>",
        self_id="1234",
    )
    assert text_mentions(event) == ["5678", "9012"]


def test_at_mentions_falls_back_to_text_markup():
    """平台没给 At 组件时，仍能从文本里的 <@id> 找到目标（昵称留空）。"""
    with_components = FakeEvent(
        self_id="1",
        messages=(FakeAt("201", "龟乙"),),
        message="<@201> 面板",
    )
    assert at_mentions(with_components) == [("201", "龟乙")]

    text_only = FakeEvent(self_id="1", message="<@201> 面板")
    assert at_mentions(text_only) == [("201", "")]


def test_at_mentions_prefers_the_component_name_and_dedupes():
    event = FakeEvent(
        self_id="1",
        messages=(FakeAt("201", "龟乙"), FakeAt("201", "龟乙")),
        message="<@201> 面板",
    )
    assert at_mentions(event) == [("201", "龟乙")]


# ---------------------------------------------------------------------------
# QQ 官方机器人：self_id 有时是被 @ 的对手
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("艾斯比", True),
        ("<@1234> 艾斯比", True),
        ("决斗 @某人", True),
        ("挑战 游走消耗", True),
        ("签到", False),
        ("今天天气不错", False),
    ],
)
def test_looks_like_duel(message: str, expected: bool):
    assert looks_like_duel(FakeEvent(message=message)) is expected


@pytest.mark.parametrize(
    ("platform", "message", "self_in_bots"),
    [
        # 非官方平台：self_id 一定是机器人
        ("aiocqhttp", "签到", True),
        ("aiocqhttp", "艾斯比", True),
        # 官方机器人 + 普通指令：self_id 当机器人（否则 <@机器人> 签到 认不出来）
        ("qq_official", "签到", True),
        # 官方机器人 + 决斗：self_id 可能是被 @ 的那个对手，不能当机器人
        ("qq_official", "艾斯比", False),
        ("qq_official", "决斗", False),
    ],
)
def test_bot_ids_handles_the_qqofficial_self_id_quirk(platform: str, message: str, self_in_bots: bool):
    event = FakeEvent(
        platform_name=platform,
        platform_id=platform,
        message=message,
        self_id="BOT",
        user_id="SENDER",
    )
    ids = bot_ids(event)
    assert ("BOT" in ids) is self_in_bots
    # 占位 id 与发送者始终算机器人/不算对手
    assert {"all", "qq_official", "SENDER"} <= ids


@pytest.mark.parametrize("value", ["qq_official", "unknown_selfid", ""])
def test_placeholder_self_ids_always_count_as_the_bot(value: str):
    assert value in bot_ids(FakeEvent(self_id=value))


def test_wake_without_the_flag_but_with_bot_markup():
    """平台既不给 At 也不置 is_at_or_wake_command 时，仍要能认出"叫了机器人"。"""
    checkin = FakeEvent(
        platform_name="qq_official",
        platform_id="qq_official",
        message="<@BOT> 签到",
        self_id="BOT",
        is_at_or_wake_command=False,
    )
    assert checkin.is_at_or_wake_command is False
    assert is_woken(checkin) is True
    assert looks_like_command(checkin, COMMANDS) is True

    # 但决斗消息里同样的 self_id 会被当成对手，于是"没叫机器人"
    duel = FakeEvent(
        platform_name="qq_official",
        platform_id="qq_official",
        message="<@BOT> 艾斯比",
        self_id="BOT",
        is_at_or_wake_command=False,
    )
    assert is_woken(duel) is False
    # 便捷唤起词不要求唤醒，所以这个场景由 main.py 的别名分支接管


def test_placeholder_at_component_counts_as_the_bot():
    event = FakeEvent(
        platform_name="qq_official",
        platform_id="qq_official",
        message="挑战",
        self_id="BOT",
        messages=(FakeAt("qq_official", "机器人"),),
    )
    assert is_woken(event) is True
    assert at_mentions(event) == []  # 占位 At 不是对手


def test_message_text_is_normalized():
    assert message_text(FakeEvent(message="  签到   今天  ")) == "签到 今天"


def test_identity_comes_from_the_event():
    event = FakeEvent(platform_id="aiocqhttp", group_id="100", user_id="200", sender_name=" 龟 甲 ")
    identity = identity_from_event(event)
    assert (identity.platform, identity.group_id, identity.user_id) == ("aiocqhttp", "100", "200")
    assert identity.nickname == "龟 甲"


def test_identity_falls_back_to_platform_name():
    event = FakeEvent(platform_id="", platform_name="napcat")
    assert identity_from_event(event).platform == "napcat"


def test_strip_mentions_removes_at_markup():
    assert strip_mentions("<@!1234> 币") == "币"
    assert strip_mentions("@龟乙(201) 币") == "币"
    assert strip_mentions('@龟乙 <qqbot-at-user id="201"/> 币') == "币"


def test_at_targets_skips_self_and_at_all():
    event = FakeEvent(self_id="1", messages=(FakeAt("1"), FakeAt("all"), FakeAt("201", "龟乙")))
    assert at_targets(event) == ["201"]


def test_ranking_args_by_coins_keyword():
    assert parse_ranking_args(FakeEvent(message="排行 币"), "币") == (True, None)
    assert parse_ranking_args(FakeEvent(message="排行"), "") == (False, None)


def test_ranking_args_with_at_target():
    event = FakeEvent(message="排行 @龟乙(201)", messages=(FakeAt("201", "龟乙"),))
    assert parse_ranking_args(event, "@龟乙(201)") == (False, "201")


def test_ranking_args_at_target_with_coins():
    event = FakeEvent(message="排行 币 @龟乙(201)", messages=(FakeAt("201", "龟乙"),))
    assert parse_ranking_args(event, "@龟乙(201) 币") == (True, "201")


async def test_whitelist_only_guards_the_handler():
    seen: list[str] = []

    class Plugin:
        def __init__(self, entries):
            self.settings = load_settings({"access": {"group_whitelist": entries}})

        @whitelist_only
        async def handler(self, event):
            seen.append("called")
            yield "ok"

    allowed = Plugin(["100"])
    denied = Plugin(["999"])

    assert [item async for item in allowed.handler(FakeEvent(group_id="100"))] == ["ok"]
    assert [item async for item in denied.handler(FakeEvent(group_id="100"))] == []
    assert seen == ["called"]


async def test_whitelist_only_requires_settings():
    class Plugin:
        @whitelist_only
        async def handler(self, event):
            yield "ok"

    assert [item async for item in Plugin().handler(FakeEvent())] == []
