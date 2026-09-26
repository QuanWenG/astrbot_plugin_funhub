"""平台禁言适配：aiocqhttp（OneBot）与 QQ 官方机器人。

QQ 官方部分按官方文档的请求体形状断言：
``POST /v2/groups/{group_openid}/restrict_chat_setting``，
``{"members": [{"op": "add", "member_openid": ..., "mute_expire_at": RFC3339}]}``；
``GET /v2/groups/{group_openid}/bot_state`` 取 ``member_role``。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from funhub.platform_api import (
    REASON_ERROR,
    REASON_NOT_ADMIN,
    REASON_PROTECTED,
    REASON_UNSUPPORTED,
    AiocqhttpGroup,
    GroupPlatform,
    MuteOutcome,
    QqOfficialGroup,
    _classify_official_error,
    group_platform_for,
)

HK = timezone(timedelta(hours=8))


class FakeOneBot:
    """记录 call_action 的假 OneBot 客户端。"""

    def __init__(self, *, role: str = "admin", shut_up_timestamp: int = 0, fail: bool = False):
        self.role = role
        self.shut_up_timestamp = shut_up_timestamp
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError("权限不足")
        if action == "get_group_member_info":
            return {"role": self.role, "shut_up_timestamp": self.shut_up_timestamp}
        return None


class FakeEvent:
    def __init__(self, *, platform: str = "aiocqhttp", bot=None, group_id: str = "100", self_id: str = "1"):
        self.bot = bot
        self._platform = platform
        self._group_id = group_id
        self._self_id = self_id

    def get_platform_name(self):
        return self._platform

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id


# ---------------------------------------------------------------------------
# OneBot
# ---------------------------------------------------------------------------


async def test_aiocqhttp_reports_bot_role():
    bot = FakeOneBot(role="admin")
    group = AiocqhttpGroup(bot, group_id="100", self_id="1")
    assert await group.bot_role() == "admin"
    assert bot.calls == [
        ("get_group_member_info", {"group_id": 100, "user_id": 1, "no_cache": True}),
    ]


async def test_aiocqhttp_role_is_unknown_when_the_call_fails():
    group = AiocqhttpGroup(FakeOneBot(fail=True), group_id="100", self_id="1")
    assert await group.bot_role() is None
    assert await group.is_muted("200") is None


async def test_aiocqhttp_reports_member_role():
    group = AiocqhttpGroup(FakeOneBot(role="owner"), group_id="100", self_id="1")
    assert await group.member_role("200") == "owner"


async def test_aiocqhttp_member_role_is_unknown_when_the_call_fails():
    group = AiocqhttpGroup(FakeOneBot(fail=True), group_id="100", self_id="1")
    assert await group.member_role("200") is None


async def test_aiocqhttp_detects_an_existing_mute():
    future = int(datetime.now().timestamp()) + 60
    muted = AiocqhttpGroup(
        FakeOneBot(shut_up_timestamp=future),
        group_id="100",
        self_id="1",
    )
    assert await muted.is_muted("200") is True

    free = AiocqhttpGroup(FakeOneBot(shut_up_timestamp=0), group_id="100", self_id="1")
    assert await free.is_muted("200") is False


async def test_aiocqhttp_mute_uses_set_group_ban():
    bot = FakeOneBot()
    group = AiocqhttpGroup(bot, group_id="100", self_id="1")
    outcome = await group.mute("200", 60)
    assert outcome.ok is True
    assert bot.calls[-1] == (
        "set_group_ban",
        {"group_id": 100, "user_id": 200, "duration": 60},
    )


async def test_aiocqhttp_mute_failure_is_an_error_not_a_crash():
    group = AiocqhttpGroup(FakeOneBot(fail=True), group_id="100", self_id="1")
    outcome = await group.mute("200", 60)
    assert outcome.ok is False
    assert outcome.reason == REASON_ERROR
    assert "权限不足" in outcome.detail


# ---------------------------------------------------------------------------
# QQ 官方机器人
# ---------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, *, payload=None, fail: Exception | None = None):
        self.payload = payload if payload is not None else {"member_role": "admin"}
        self.fail = fail
        self.requests: list[tuple[str, str, dict | None]] = []

    async def request(self, route, json=None):
        self.requests.append((route.method, route.path, json))
        if self.fail is not None:
            raise self.fail
        return self.payload


class FakeRoute:
    def __init__(self, method: str, path: str, **params):
        self.method = method
        self.path = path
        self.params = params


class FakeBot:
    def __init__(self, http: FakeHttp):
        self.api = types.SimpleNamespace(_http=http)


@pytest.fixture
def fake_botpy(monkeypatch: pytest.MonkeyPatch):
    """把 botpy.http.Route 顶出来（真实环境由官方适配器的依赖提供）。"""
    module = types.ModuleType("botpy.http")
    module.Route = FakeRoute  # type: ignore[attr-defined]
    package = types.ModuleType("botpy")
    package.http = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botpy", package)
    monkeypatch.setitem(sys.modules, "botpy.http", module)
    return module


async def test_qqofficial_reads_member_role(fake_botpy):
    http = FakeHttp(payload={"member_role": "member"})
    group = QqOfficialGroup(FakeBot(http), group_id="GROUP", zone=HK)
    assert await group.bot_role() == "member"
    assert http.requests == [("GET", "/v2/groups/{group_openid}/bot_state", None)]


async def test_qqofficial_role_is_unknown_when_the_whitelist_api_is_denied(fake_botpy):
    http = FakeHttp(fail=RuntimeError("11253 应用无接口访问权限"))
    group = QqOfficialGroup(FakeBot(http), group_id="GROUP", zone=HK)
    assert await group.bot_role() is None


async def test_qqofficial_mute_payload_matches_the_documented_shape(fake_botpy):
    http = FakeHttp(payload={})
    group = QqOfficialGroup(FakeBot(http), group_id="GROUP", zone=HK)

    outcome = await group.mute("MEMBER", 60)

    assert outcome.ok is True
    method, path, payload = http.requests[-1]
    assert (method, path) == ("POST", "/v2/groups/{group_openid}/restrict_chat_setting")
    members = payload["members"]
    assert len(members) == 1
    assert members[0]["op"] == "add"
    assert members[0]["member_openid"] == "MEMBER"
    expire_at = datetime.fromisoformat(members[0]["mute_expire_at"])
    assert expire_at.tzinfo is not None
    delta = expire_at - datetime.now(HK)
    assert timedelta(seconds=50) < delta <= timedelta(seconds=61)


async def test_qqofficial_cannot_query_member_mute_state(fake_botpy):
    group = QqOfficialGroup(FakeBot(FakeHttp()), group_id="GROUP", zone=HK)
    assert await group.is_muted("MEMBER") is None
    # 也查不到别人的群内角色，只能靠禁言失败时的错误归类
    assert await group.member_role("MEMBER") is None


async def test_default_platform_reports_nothing():
    group = GroupPlatform()
    assert await group.bot_role() is None
    assert await group.member_role("200") is None
    assert await group.is_muted("200") is None


async def test_qqofficial_mute_failure_is_reported(fake_botpy):
    http = FakeHttp(fail=RuntimeError("该成员是管理员，无法禁言"))
    group = QqOfficialGroup(FakeBot(http), group_id="GROUP", zone=HK)
    outcome = await group.mute("MEMBER", 60)
    assert outcome.ok is False
    assert outcome.reason == REASON_PROTECTED


def test_official_error_classification():
    assert _classify_official_error(RuntimeError("不能操作群主/管理员")) == REASON_PROTECTED
    assert _classify_official_error(RuntimeError("11253 应用无接口访问权限")) == REASON_NOT_ADMIN
    assert _classify_official_error(RuntimeError("连接超时")) == REASON_ERROR


# ---------------------------------------------------------------------------
# 工厂与降级
# ---------------------------------------------------------------------------


async def test_default_platform_does_not_support_mute():
    group = GroupPlatform()
    assert await group.bot_role() is None
    assert await group.mute("200", 60) == MuteOutcome(False, REASON_UNSUPPORTED)


def test_factory_picks_aiocqhttp():
    event = FakeEvent(platform="aiocqhttp", bot=FakeOneBot())
    assert isinstance(group_platform_for(event, zone=HK), AiocqhttpGroup)


@pytest.mark.parametrize("platform", ["qqofficial", "qq_official_webhook"])
def test_factory_picks_qqofficial(platform: str):
    event = FakeEvent(platform=platform, bot=FakeBot(FakeHttp()))
    assert isinstance(group_platform_for(event, zone=HK), QqOfficialGroup)


@pytest.mark.parametrize(
    "event",
    [
        FakeEvent(platform="telegram", bot=FakeOneBot()),
        FakeEvent(platform="aiocqhttp", bot=None),
        FakeEvent(platform="aiocqhttp", bot=FakeOneBot(), group_id=""),
    ],
)
def test_factory_degrades_to_unsupported(event: FakeEvent):
    assert isinstance(group_platform_for(event, zone=HK), GroupPlatform)
    assert type(group_platform_for(event, zone=HK)) is GroupPlatform
