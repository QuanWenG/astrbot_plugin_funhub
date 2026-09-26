"""白名单、事件身份与指令文本处理。

这一层刻意不 import astrbot：函数只依赖鸭子类型的 ``event``，测试里传一个带
``get_group_id`` / ``get_message_str`` 等方法的假对象即可。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from functools import wraps
from typing import Any

from .commands import DUEL_WORDS
from .players import PlayerIdentity

#: 唤醒后残留的 @ 标记：QQ 官方 ``<@!id>``、QQBot ``<qqbot-at-user id="..."/>``，
#: 以及 aiocqhttp 把非首个 @ 写进文本的 ``@昵称(qq)`` 形式。
MENTION_MARKUP_PATTERN = re.compile(
    r"(?:<@!?(?P<legacy_id>[^>\s]+)>|"
    r"<qqbot-at-user\b[^>]*\bid\s*=\s*[\"']?(?P<qqbot_id>[^\"'\s/>]+)[\"']?[^>]*/?>)",
    re.IGNORECASE,
)
AT_TEXT_PATTERN = re.compile(r"@[^\s(（]*[（(]\d+[)）]|@\S+")

#: 指令名前面的"装饰前缀"：插件叫龟龟乐园、币叫龟龟币，玩家顺手就会打成
#: ``龟龟轮盘``。它不是指令名（指令是朴素功能名），但值得容忍。
DECORATIONS = ("龟龟",)

#: 指令名后面最多还能跟几个字，才算"想用指令但打错了"
#: （``轮盘啦`` 算，``面板挺好看的`` 就不猜了）
HINT_MAX_TAIL = 4


def _clean(value: object) -> str:
    return str(value or "").strip()


def whitelist_candidates(
    *,
    platform_id: str,
    platform_name: str,
    group_id: str,
    unified_msg_origin: str,
) -> set[str]:
    """事件可能被白名单命中的几种写法。"""
    candidates: set[str] = set()
    group = _clean(group_id)
    if group:
        candidates.add(group)
        for prefix in (_clean(platform_id), _clean(platform_name)):
            if prefix:
                candidates.add(f"{prefix}:{group}")
    origin = _clean(unified_msg_origin)
    if origin:
        candidates.add(origin)
    return candidates


def allows(
    entries: Iterable[str],
    *,
    platform_id: str,
    platform_name: str,
    group_id: str,
    unified_msg_origin: str,
) -> bool:
    """群是否在白名单内。空名单一律拒绝，私聊（无群号）一律拒绝。"""
    allowed = {_clean(item) for item in entries if _clean(item)}
    if not allowed:
        return False
    return bool(
        allowed.intersection(
            whitelist_candidates(
                platform_id=platform_id,
                platform_name=platform_name,
                group_id=group_id,
                unified_msg_origin=unified_msg_origin,
            ),
        ),
    )


def identity_from_event(event: Any) -> PlayerIdentity:
    """从事件里取身份三元组。"""
    platform = _clean(
        _call(event, "get_platform_id") or _call(event, "get_platform_name") or "unknown",
    )
    return PlayerIdentity(
        platform=platform,
        group_id=_clean(_call(event, "get_group_id")),
        user_id=_clean(_call(event, "get_sender_id") or _call(event, "get_session_id")),
        nickname=" ".join(_clean(_call(event, "get_sender_name")).split()),
    )


def message_text(event: Any) -> str:
    """唤醒前缀已被 AstrBot 剥离后的纯文本（本项目统一按已剥离处理）。"""
    return " ".join(_clean(_call(event, "get_message_str")).split())


#: 平台没能认出机器人自己时使用的占位 id（老插件同样把它们当作"机器人"）。
PLACEHOLDER_SELF_IDS = frozenset({"", "all", "qq_official", "qqofficial", "unknown_selfid"})


def platform_name(event: Any) -> str:
    return _clean(_call(event, "get_platform_name")) or _clean(
        _call(event, "get_platform_id"),
    )


def looks_like_duel(event: Any) -> bool:
    """这条消息是不是在发起决斗。

    QQ 官方机器人上 ``self_id`` 可能是被 @ 的对手，只有"看起来像决斗"时才能这么
    认定（老插件 ``_is_bot_target_id``）：所以先去 @ 标记看指令，再看看全文里有没有
    挑战唤起词（``艾斯比`` 之类允许出现在句子中间）。
    """
    text = strip_mentions(message_text(event))
    if not text:
        return any(word in message_text(event) for word in DUEL_WORDS)
    for word in DUEL_WORDS:
        if text == word or text.startswith(f"{word} "):
            return True
    return any(word in text for word in DUEL_WORDS)


def bot_ids(event: Any) -> set[str]:
    """哪些 id 应当被当成"机器人自己"。

    沿用老插件 ``_ignored_target_ids`` 的口径：

    * 占位 id（``qq_official`` 等）一定是机器人；
    * 发送者自己不算对手；
    * ``self_id``：非 qq_official 平台无条件算机器人；**qq_official 上要看消息**——
      ``self_id`` 经常就是那个被 @ 的对手（老插件注释：*Account for QQ Official events
      that expose the target At as self_id*），所以决斗消息里的 ``self_id`` 不算机器人，
      普通指令（``<@机器人> 签到``）里才算。
    """
    ignored = set(PLACEHOLDER_SELF_IDS)
    sender_id = _clean(_call(event, "get_sender_id"))
    if sender_id:
        ignored.add(sender_id)
    self_id = _clean(_call(event, "get_self_id"))
    if not self_id or self_id in PLACEHOLDER_SELF_IDS:
        return ignored
    if platform_name(event) != "qq_official" or not looks_like_duel(event):
        ignored.add(self_id)
    return ignored


def mentions_bot(event: Any) -> bool:
    """这条消息里的 @ 是否指向机器人。

    同时看 ``At`` 组件与文本里的 ``<@id>``：QQ 官方机器人两种形态都可能出现，而且
    ``At(qq="qq_official")`` 这种占位也代表机器人。
    """
    ignored = bot_ids(event)
    for component in _messages(event):
        qq = _clean(getattr(component, "qq", None))
        if qq and qq in ignored:
            return True
    return any(user_id in ignored for user_id in markup_ids(event))


def is_woken(event: Any) -> bool:
    """这条消息算不算"叫了机器人"。

    AstrBot 的 ``is_at_or_wake_command`` 只在命中唤醒前缀或存在指向机器人的 ``At``
    组件时为真；插件 handler 被过滤器激活时并不会置真（``waking_check/stage.py`` 只设
    ``event.is_wake``），QQ 官方机器人还常常既不给 ``At`` 组件、也不把 ``<@机器人id>``
    从文本里去掉。所以这里自己再判一次。
    """
    if getattr(event, "is_at_or_wake_command", False):
        return True
    return mentions_bot(event)


def markup_ids(event: Any) -> list[str]:
    """文本里所有 ``<@id>`` / ``<qqbot-at-user id="..."/>`` 形式的 id（不过滤）。"""
    raw = message_text(event)
    if not raw:
        return []
    return [
        user_id
        for match in MENTION_MARKUP_PATTERN.finditer(raw)
        if (user_id := _clean(match.group("legacy_id") or match.group("qqbot_id")))
    ]


def looks_like_command(
    event: Any,
    names: Sequence[str],
    *,
    wildcard: Sequence[str] = (),
) -> bool:
    """判断事件是否是本插件的指令。

    与 AstrBot ``CommandFilter`` 用同一套匹配规则（完全相等，或以 ``指令名 + 空格``
    开头），但**先去掉 @ 标记**，并按 :func:`is_woken` 判断唤醒：这样 ``签到`` 命中，
    ``签到啦`` / ``排行币`` 不命中，``<@机器人> 签到`` 也能命中。

    ``wildcard`` 里的名字额外允许出现在句子任何位置（见 :func:`parse_command`）。
    """
    if not is_woken(event):
        return False
    return parse_command(event, names, wildcard=wildcard) is not None


def _command_variants(text: str, decorations: Sequence[str]) -> list[str]:
    """原文，以及去掉装饰前缀后的写法。"""
    variants = [text]
    for decoration in decorations:
        if text.startswith(decoration) and len(text) > len(decoration):
            variants.append(text[len(decoration) :].strip())
    return variants


def parse_command(
    event: Any,
    names: Sequence[str],
    *,
    wildcard: Sequence[str] = (),
    decorations: Sequence[str] = DECORATIONS,
) -> tuple[str, str] | None:
    """从事件文本里解析出 ``(指令名, 其余参数)``。

    唤醒前缀已由 AstrBot 剥掉；这里再去掉 @ 标记（``<@id>`` / ``@昵称(qq)``），
    所以 ``<@对手> 艾斯比`` 这种"@ 在指令前面"的写法也能解析出来。

    Args:
        names: 允许的指令名。
        wildcard: 允许出现在句子**任何位置**的名字（老插件对挑战唤起词就是这么匹配的），
            例如 ``艾斯比吧你`` / ``来决斗啊``。句中出现多个时取最早的那个。
        decorations: 允许被忽略的前缀（见 :data:`DECORATIONS`）。
    """
    text = strip_mentions(message_text(event))
    if not text:
        return None
    variants = _command_variants(text, decorations)
    for candidate in variants:
        for name in names:
            if candidate == name:
                return name, ""
            if candidate.startswith(f"{name} "):
                return name, candidate[len(name) :].strip()

    earliest: tuple[int, str, str] | None = None
    for candidate in variants:
        for name in wildcard:
            index = candidate.find(name)
            if index < 0:
                continue
            if earliest is None or index < earliest[0]:
                earliest = (index, name, candidate)
    if earliest is None:
        return None
    index, name, matched = earliest
    return name, matched[index + len(name) :].strip()


def near_miss_command(
    event: Any,
    names: Sequence[str],
    *,
    max_tail: int = HINT_MAX_TAIL,
    decorations: Sequence[str] = DECORATIONS,
) -> str | None:
    """叫了机器人、开头就是指令名，但整句并不是合法指令。

    用途只有一个：别让"打错指令"表现成"插件死了"。返回命中的那个名字，
    调用方用 :func:`funhub.commands.canonical_command` 归一后再提示。

    判据刻意收得很紧 —— 指令名必须在最前面，后面最多再跟 ``max_tail`` 个字
    （``轮盘啦``、``艾斯比吧你`` 算；闲聊里提一句"面板"不算），
    所以只有"本来就想输入指令"的消息会被纠正。
    """
    if not is_woken(event):
        return None
    text = strip_mentions(message_text(event))
    if not text:
        return None
    for candidate in _command_variants(text, decorations):
        for name in names:
            if candidate.startswith(name) and len(candidate) - len(name) <= max_tail:
                return name
    return None


def strip_mentions(text: str) -> str:
    """去掉 @ 标记，留下可用的命令与参数文本。"""
    text = MENTION_MARKUP_PATTERN.sub(" ", text)
    text = AT_TEXT_PATTERN.sub(" ", text)
    return " ".join(text.split())


def text_mentions(event: Any) -> list[str]:
    """从文本里挖出 ``<@id>`` 形式的被 @ 用户（排除机器人与 @全体）。"""
    ignored = bot_ids(event)
    found: list[str] = []
    for user_id in markup_ids(event):
        if user_id in ignored or user_id in found:
            continue
        found.append(user_id)
    return found


def at_mentions(event: Any) -> list[tuple[str, str]]:
    """事件里被 @ 的其他用户 ``(id, 昵称)``，排除机器人、@全体与发送者。

    昵称来自群名片；``At`` 组件缺失（QQ 官方机器人的常见形态）时回落到文本里的
    ``<@id>``，此时昵称未知。
    """
    ignored = bot_ids(event)
    mentions: list[tuple[str, str]] = []
    seen: set[str] = set()
    for component in _messages(event):
        qq = _clean(getattr(component, "qq", None))
        if not qq or qq in ignored or qq in seen:
            continue
        seen.add(qq)
        mentions.append((qq, " ".join(_clean(getattr(component, "name", None)).split())))
    for user_id in text_mentions(event):
        if user_id not in seen:
            seen.add(user_id)
            mentions.append((user_id, ""))
    return mentions


def at_targets(event: Any) -> list[str]:
    """事件里被 @ 的其他用户 id。"""
    return [user_id for user_id, _ in at_mentions(event)]


def parse_ranking_args(event: Any, args: str) -> tuple[bool, str | None]:
    """解析 ``排行`` 的参数。

    Returns:
        ``(是否按币排序, 目标用户 id 或 None)``。
    """
    by_coins = False
    for token in strip_mentions(args or "").split():
        if token in {"币", "龟龟币", "钱"}:
            by_coins = True
    targets = at_targets(event)
    return by_coins, targets[0] if targets else None


def target_identity(event: Any, user_id: str) -> PlayerIdentity:
    """把被 @ 的用户拼成同群身份；昵称留空，展示时回落到库里已有昵称。"""
    own = identity_from_event(event)
    return PlayerIdentity(
        platform=own.platform,
        group_id=own.group_id,
        user_id=user_id,
        nickname="",
    )


def in_whitelist(settings: Any, event: Any) -> bool:
    return allows(
        settings.access.group_whitelist,
        platform_id=_clean(_call(event, "get_platform_id")),
        platform_name=_clean(_call(event, "get_platform_name")),
        group_id=_clean(_call(event, "get_group_id")),
        unified_msg_origin=_clean(getattr(event, "unified_msg_origin", "")),
    )


def whitelist_only(
    handler: Callable[..., AsyncGenerator[Any, None]],
) -> Callable[..., AsyncGenerator[Any, None]]:
    """白名单守卫装饰器：不在名单内的会话直接静默返回。"""

    @wraps(handler)
    async def guarded(self, event, *args, **kwargs):
        settings = getattr(self, "settings", None)
        if settings is None or not in_whitelist(settings, event):
            return
        async for result in handler(self, event, *args, **kwargs):
            yield result

    guarded.__whitelist_guarded__ = True  # type: ignore[attr-defined]
    return guarded


# ---------------------------------------------------------------------------
# 鸭子类型取值
# ---------------------------------------------------------------------------


def _call(event: Any, name: str) -> Any:
    method = getattr(event, name, None)
    if method is None:
        return None
    try:
        value = method() if callable(method) else method
    except Exception:  # noqa: BLE001 - 事件适配器差异不该让指令崩掉
        return None
    if inspect.isawaitable(value):  # pragma: no cover - 这些取值方法都是同步的
        return None
    return value


def _messages(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    if getter is None:
        return []
    try:
        messages = getter()
    except Exception:  # noqa: BLE001
        return []
    return list(messages) if isinstance(messages, (list, tuple)) else []
