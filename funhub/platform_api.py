"""群平台能力：机器人身份、成员禁言状态、禁言/解禁。

这里只依赖鸭子类型的 ``event``（``get_platform_name`` / ``bot`` / ``get_self_id``），
不 import astrbot，因此可以在测试里用假事件替换。

两套实现对应两类适配器：

* ``aiocqhttp``（OneBot v11）：``set_group_ban`` / ``get_group_member_info``。
* ``qqofficial``（QQ 官方机器人）：官方 HTTP 接口
  ``POST /v2/groups/{group_openid}/restrict_chat_setting``（设置群成员禁言）与
  ``GET  /v2/groups/{group_openid}/bot_state``（获取机器人群内状态，含 ``member_role``）。
  官方接口要求机器人是群管理员，且**只能禁言普通成员**（群主 / 管理员 / 机器人禁不了），
  这正是"禁言失败"的来源。``bot_state`` 属于白名单接口（错误码 11253），拿不到时
  角色视为未知。

平台不支持或查不到时一律返回"未知"，由上层按保守策略处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

#: 群内管理身份
ADMIN_ROLES = frozenset({"admin", "owner"})

#: 平台不支持禁言
REASON_UNSUPPORTED = "unsupported"
#: 机器人不是管理员
REASON_NOT_ADMIN = "not_admin"
#: 对方是群主 / 管理员 / 机器人，禁不了
REASON_PROTECTED = "protected"
#: 其它失败（网络、权限、接口未开放等）
REASON_ERROR = "error"


@dataclass(frozen=True, slots=True)
class MuteOutcome:
    ok: bool
    reason: str = ""
    detail: str = ""


class GroupPlatform:
    """默认实现：平台没有可用的禁言能力。"""

    name = "unsupported"

    async def bot_role(self) -> str | None:
        """机器人在该群的角色：``owner`` / ``admin`` / ``member``；未知返回 None。"""
        return None

    async def member_role(self, user_id: str) -> str | None:
        """某个成员在该群的角色；平台查不到返回 None。

        用来判断"这个人是不是管理员/群主，根本禁不了言"。
        """
        del user_id
        return None

    async def is_muted(self, user_id: str) -> bool | None:
        """成员是否正在禁言中；平台查不到返回 None。"""
        del user_id
        return None

    async def mute(self, user_id: str, seconds: int) -> MuteOutcome:
        del user_id, seconds
        return MuteOutcome(False, REASON_UNSUPPORTED)


class AiocqhttpGroup(GroupPlatform):
    """OneBot v11（aiocqhttp / NapCat / Lagrange 等）。"""

    name = "aiocqhttp"

    def __init__(self, bot: Any, *, group_id: str, self_id: str) -> None:
        self._bot = bot
        self._group_id = group_id
        self._self_id = self_id

    async def _member_info(self, user_id: str) -> dict[str, Any] | None:
        try:
            info = await self._bot.call_action(
                "get_group_member_info",
                group_id=int(self._group_id),
                user_id=int(user_id),
                no_cache=True,
            )
        except Exception:  # noqa: BLE001 - 平台异常不该让决斗崩掉
            return None
        return info if isinstance(info, dict) else None

    async def bot_role(self) -> str | None:
        if not self._self_id:
            return None
        info = await self._member_info(self._self_id)
        if not info:
            return None
        role = str(info.get("role") or "").strip()
        return role or None

    async def member_role(self, user_id: str) -> str | None:
        info = await self._member_info(user_id)
        if not info:
            return None
        role = str(info.get("role") or "").strip()
        return role or None

    async def is_muted(self, user_id: str) -> bool | None:
        info = await self._member_info(user_id)
        if not info:
            return None
        until = info.get("shut_up_timestamp")
        try:
            return int(until or 0) > int(time.time())
        except (TypeError, ValueError):
            return None

    async def mute(self, user_id: str, seconds: int) -> MuteOutcome:
        """禁言；``seconds <= 0`` 表示解除禁言（OneBot 用 ``duration=0``）。"""
        try:
            await self._bot.call_action(
                "set_group_ban",
                group_id=int(self._group_id),
                user_id=int(user_id),
                duration=max(int(seconds), 0),
            )
        except Exception as exc:  # noqa: BLE001
            return MuteOutcome(False, REASON_ERROR, _short(exc))
        return MuteOutcome(True)


class QqOfficialGroup(GroupPlatform):
    """QQ 官方机器人（qqofficial / qqofficial_webhook）。"""

    name = "qqofficial"

    def __init__(
        self,
        bot: Any,
        *,
        group_id: str,
        zone: tzinfo,
    ) -> None:
        self._bot = bot
        self._group_id = group_id
        self._zone = zone

    async def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        try:
            from botpy.http import Route
        except ImportError as exc:  # pragma: no cover - 只有官方机器人适配器装了 botpy
            raise RuntimeError("缺少 botpy，无法调用 QQ 官方接口") from exc
        route = Route(method, path, group_openid=self._group_id)
        http = getattr(getattr(self._bot, "api", None), "_http", None)
        if http is None:  # pragma: no cover - 防御
            raise RuntimeError("QQ 官方客户端未提供 HTTP 会话")
        if payload is None:
            return await http.request(route)
        return await http.request(route, json=payload)

    async def bot_role(self) -> str | None:
        try:
            data = await self._request("GET", "/v2/groups/{group_openid}/bot_state")
        except Exception:  # noqa: BLE001 - 白名单接口，拿不到就算了
            return None
        if not isinstance(data, dict):
            return None
        role = str(data.get("member_role") or "").strip()
        return role or None

    async def is_muted(self, user_id: str) -> bool | None:
        del user_id
        # 官方接口按群查询禁言状态，查不到"某个成员是否被禁言"。
        return None

    async def member_role(self, user_id: str) -> str | None:
        del user_id
        # 官方接口只给"机器人自己"的群内角色，别人的角色查不到，
        # 只能靠禁言失败时的错误归类（见 _classify_official_error）。
        return None

    async def mute(self, user_id: str, seconds: int) -> MuteOutcome:
        """禁言；``seconds <= 0`` 表示解除禁言。"""
        seconds = int(seconds)
        if seconds > 0:
            expire_at = (
                datetime.now(self._zone) + timedelta(seconds=seconds)
            ).isoformat(timespec="seconds")
            payload = {
                "members": [
                    {
                        "op": "add",
                        "member_openid": user_id,
                        "mute_expire_at": expire_at,
                    },
                ],
            }
        else:
            # op=del + 空字符串 = 立即解除禁言
            payload = {
                "members": [{"op": "del", "member_openid": user_id, "mute_expire_at": ""}],
            }
        try:
            await self._request(
                "POST",
                "/v2/groups/{group_openid}/restrict_chat_setting",
                payload,
            )
        except Exception as exc:  # noqa: BLE001
            return MuteOutcome(False, _classify_official_error(exc), _short(exc))
        return MuteOutcome(True)


#: 官方接口把"对方不能被禁言"和"机器人没有权限"混在错误信息里，只能按关键词归类。
_PROTECTED_HINTS = ("管理员", "群主", "机器人", "admin", "owner", "manage")
_PERMISSION_HINTS = ("权限", "无接口访问权限", "11253", "forbidden", "permission")


def _classify_official_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(hint.lower() in text for hint in _PROTECTED_HINTS):
        return REASON_PROTECTED
    if any(hint.lower() in text for hint in _PERMISSION_HINTS):
        return REASON_NOT_ADMIN
    return REASON_ERROR


def _short(exc: BaseException, limit: int = 120) -> str:
    return " ".join(str(exc).split())[:limit]


def group_platform_for(event: Any, *, zone: tzinfo) -> GroupPlatform:
    """按事件所属平台挑一个实现；未知平台返回不支持禁言的空实现。"""
    name = ""
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            name = str(getter() or "")
        except Exception:  # noqa: BLE001
            name = ""
    bot = getattr(event, "bot", None)
    group_id = ""
    group_getter = getattr(event, "get_group_id", None)
    if callable(group_getter):
        try:
            group_id = str(group_getter() or "")
        except Exception:  # noqa: BLE001
            group_id = ""
    self_id = ""
    self_getter = getattr(event, "get_self_id", None)
    if callable(self_getter):
        try:
            self_id = str(self_getter() or "")
        except Exception:  # noqa: BLE001
            self_id = ""

    if bot is None or not group_id:
        return GroupPlatform()
    if name == "aiocqhttp":
        return AiocqhttpGroup(bot, group_id=group_id, self_id=self_id)
    if name in {"qqofficial", "qq_official", "qq_official_webhook", "qqofficial_webhook"}:
        return QqOfficialGroup(bot, group_id=group_id, zone=zone)
    return GroupPlatform()
