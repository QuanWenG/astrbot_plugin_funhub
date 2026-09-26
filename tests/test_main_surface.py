"""用假的 astrbot 包导入 main.py，验证与宿主的接线。

测试环境里没有 AstrBot（它是宿主应用），但 main.py 一旦有装饰器签名写错、路由没
注册、属性名打错，插件在真实环境里就会加载失败。这里用最小的桩模块把
``astrbot.api`` / ``astrbot.core`` 顶出来，真实执行 main.py 的模块级代码。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from .conftest import FakeAt

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
PACKAGE = "funhub_plugin_under_test"


class Registry:
    def __init__(self) -> None:
        self.commands: list[tuple[str, set[str]]] = []
        self.events: list[dict] = []
        self.llm_hooks: list[dict] = []
        self.web_apis: list[tuple[str, list[str]]] = []


@pytest_asyncio.fixture
async def plugin_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """构造 astrbot 桩模块并导入 main.py，返回 (模块, 注册表, 数据目录)。"""
    registry = Registry()
    data_dir = tmp_path / "plugin_data"
    data_dir.mkdir(parents=True, exist_ok=True)

    def module(name: str) -> types.ModuleType:
        created = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, created)
        return created

    astrbot = module("astrbot")
    api = module("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    astrbot.api = api

    class FakeFilter:
        class EventMessageType:
            GROUP_MESSAGE = "group_message"
            ALL = "all"

        def command(self, name, alias=None, **kwargs):
            def decorator(func):
                registry.commands.append((name, set(alias or ())))
                return func

            return decorator

        def event_message_type(self, *args, **kwargs):
            def decorator(func):
                registry.events.append({"args": args, "kwargs": kwargs})
                return func

            return decorator

        def on_waiting_llm_request(self, **kwargs):
            def decorator(func):
                registry.llm_hooks.append(kwargs)
                return func

            return decorator

    event_module = module("astrbot.api.event")
    event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_module.filter = FakeFilter()
    api.event = event_module

    class Star:
        def __init__(self, context=None, config=None) -> None:
            self.context = context
            self.config = config

    class Context:
        def __init__(self, *, stars=None) -> None:
            #: 插件表（真实的宿主用 ``context.get_all_stars()`` 给）
            self.stars = list(stars or [])

        def register_web_api(self, route, handler, methods, desc) -> None:
            registry.web_apis.append((route, list(methods)))

        def get_all_stars(self):
            return list(self.stars)

    class StarTools:
        @staticmethod
        def get_data_dir(name=None) -> Path:
            return data_dir

    star_module = module("astrbot.api.star")
    star_module.Star = Star
    star_module.Context = Context
    star_module.StarTools = StarTools
    star_module.register = lambda *a, **k: (lambda cls: cls)
    api.star = star_module

    def json_response(data=None, **kwargs):
        return {"kind": "json", "data": data, "status_code": kwargs.get("status_code", 200)}

    def error_response(message, **kwargs):
        return {
            "kind": "error",
            "message": message,
            "status_code": kwargs.get("status_code", 400),
        }

    web_module = module("astrbot.api.web")
    web_module.PluginUploadFile = type("PluginUploadFile", (), {})
    web_module.json_response = json_response
    web_module.error_response = error_response
    web_module.request = types.SimpleNamespace()
    web_module.file_response = lambda *a, **k: None
    api.web = web_module

    core = module("astrbot.core")
    core_star = module("astrbot.core.star")
    core_star_filter = module("astrbot.core.star.filter")
    command_filter = module("astrbot.core.star.filter.command")

    class GreedyStr(str):
        pass

    command_filter.GreedyStr = GreedyStr
    core.star = core_star
    core_star.filter = core_star_filter
    core_star_filter.command = command_filter
    astrbot.core = core

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, PACKAGE, package)

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.main",
        MAIN,
        submodule_search_locations=[str(ROOT)],
    )
    assert spec and spec.loader
    module_obj = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.main", module_obj)
    spec.loader.exec_module(module_obj)

    # 包一层：记住测试里建过的插件实例，收尾时统一关连接。
    # 否则某个断言失败会让 terminate() 跑不到，aiosqlite 的工作线程留着不放，
    # pytest 跑完测试却退不出进程（表现为"卡住"）。
    created: list[object] = []
    plugin_cls = module_obj.FunHubPlugin

    class TrackedPlugin(plugin_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    module_obj.FunHubPlugin = TrackedPlugin
    try:
        yield module_obj, registry, data_dir
    finally:
        for plugin in created:
            try:
                await plugin.terminate()
            except Exception:  # noqa: BLE001 - 收尾失败不该盖住测试结果
                pass


def test_commands_are_registered_with_aliases(plugin_module):
    module, registry, _ = plugin_module
    commands = dict(registry.commands)
    assert set(commands) == {
        "签到",
        "面板",
        "排行",
        "决斗",
        "轮盘",
        "开枪",
        "救一下",
        "补一枪",
    }
    for name in commands:
        assert commands[name] == module.COMMAND_ALIASES[name]


def test_auto_checkin_and_llm_guard_are_registered(plugin_module):
    module, registry, _ = plugin_module
    # 自动签到（群消息）+ @ 唤醒的指令兜底通道
    assert len(registry.events) == 2
    priorities = sorted(item["kwargs"]["priority"] for item in registry.events)
    assert priorities == [90, 100]
    assert registry.llm_hooks == [{"priority": 1000}]
    assert module.ALL_COMMAND_NAMES[0] == "签到"
    assert "排行榜" in module.ALL_COMMAND_NAMES
    assert module.canonical_command("艾斯比") == "决斗"
    assert module.canonical_command("决斗") == "决斗"


def test_web_routes_are_registered(plugin_module):
    module, registry, _ = plugin_module
    # 路由在插件实例化时注册（与官方 Pages 文档一致）
    module.FunHubPlugin(context=module.Context(), config={})
    routes = dict(registry.web_apis)
    assert set(routes) == {
        f"/{module.PLUGIN_NAME}/import/list",
        f"/{module.PLUGIN_NAME}/import/upload",
        f"/{module.PLUGIN_NAME}/import/preview",
        f"/{module.PLUGIN_NAME}/import/apply",
    }
    assert routes[f"/{module.PLUGIN_NAME}/import/list"] == ["GET"]
    assert routes[f"/{module.PLUGIN_NAME}/import/apply"] == ["POST"]


async def test_plugin_initializes_and_serves_import_list(plugin_module, monkeypatch, tmp_path):
    module, _, data_dir = plugin_module
    plugin = module.FunHubPlugin(context=module.Context(), config={})

    await plugin.initialize()
    assert (data_dir / "funhub.db").is_file()
    assert (data_dir / "import").is_dir()

    response = await plugin.web_import_list()
    assert response["kind"] == "json"
    assert response["data"] == {"files": [], "last_report": None}

    await plugin.terminate()


async def test_upload_endpoint_previews_and_rejects_junk(plugin_module):
    module, _, data_dir = plugin_module
    plugin = module.FunHubPlugin(context=module.Context(), config={})
    await plugin.initialize()

    payload = (
        Path(__file__).resolve().parent / "fixtures" / "legacy_export_sample.json"
    ).read_bytes()

    class Upload(module.PluginUploadFile):  # type: ignore[misc]
        filename = "export.json"

        async def read(self) -> bytes:
            return payload

    upload = Upload()

    async def files():
        return {"file": upload}

    module.request.files = files
    response = await plugin.web_import_upload()
    assert response["kind"] == "json"
    assert response["data"]["preview"]["coins_granted"] == 2645
    assert response["data"]["filename"] == "export.json"

    async def empty_files():
        return {}

    module.request.files = empty_files
    rejected = await plugin.web_import_upload()
    assert rejected["kind"] == "error"
    assert rejected["status_code"] == 400

    await plugin.terminate()


async def test_apply_endpoint_imports_then_reports(plugin_module):
    module, _, data_dir = plugin_module
    plugin = module.FunHubPlugin(context=module.Context(), config={})
    await plugin.initialize()

    payload = (
        Path(__file__).resolve().parent / "fixtures" / "legacy_export_sample.json"
    ).read_bytes()

    class Upload(module.PluginUploadFile):  # type: ignore[misc]
        filename = "export.json"

        async def read(self) -> bytes:
            return payload

    async def files():
        return {"file": Upload()}

    module.request.files = files
    await plugin.web_import_upload()

    async def body(default=None):
        return {"filename": "export.json"}

    module.request.json = body
    response = await plugin.web_import_apply()
    assert response["kind"] == "json"
    assert response["data"]["coins_granted"] == 2645
    assert response["data"]["dry_run"] is False

    listing = await plugin.web_import_list()
    assert listing["data"]["last_report"]["coins_granted"] == 2645

    await plugin.terminate()


async def test_whitelist_gate_blocks_other_groups(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=types.SimpleNamespace(),
        config={"access": {"group_whitelist": ["999"]}},
    )

    from funhub.access import in_whitelist

    class Event:
        is_at_or_wake_command = True
        unified_msg_origin = "aiocqhttp:GroupMessage:100"

        def get_message_str(self):
            return "签到"

        def get_group_id(self):
            return "100"

        def get_platform_id(self):
            return "aiocqhttp"

        def get_platform_name(self):
            return "aiocqhttp"

    assert not in_whitelist(plugin.settings, Event())


class GroupEvent:
    """最小的事件替身，覆盖"群里发了个纯表情"这种情况：message_str 为空。"""

    def __init__(
        self,
        *,
        message: str = "",
        group_id: str = "100",
        user_id: str = "200",
        sender_name: str = "龟甲",
        woken: bool = False,
    ) -> None:
        self._message = message
        self._group_id = group_id
        self._user_id = user_id
        self._sender_name = sender_name
        self.is_at_or_wake_command = woken
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"
        self.stopped = False
        self.replies: list[str] = []
        self._extras: dict = {}

    def get_message_str(self):
        return self._message

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._user_id

    def get_self_id(self):
        return "1"

    def get_platform_id(self):
        return "aiocqhttp"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_name(self):
        return self._sender_name

    def get_session_id(self):
        return f"{self._group_id}:{self._user_id}"

    def get_messages(self):
        return []

    def get_extra(self, key=None, default=None):
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def plain_result(self, text):
        self.replies.append(text)
        return text

    def stop_event(self):
        self.stopped = True


async def test_auto_checkin_runs_for_an_emoji_only_message(plugin_module):
    """用户报的场景：群里发了个表情，应当自动签到。

    纯表情消息的 message_str 是空字符串、也没有被唤醒，自动签到必须照样生效。
    """
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = GroupEvent(message="")  # 表情消息没有文本
    replies = [item async for item in plugin.auto_check_in(event)]

    row = await plugin.database.fetchone("SELECT * FROM players WHERE user_id = '200'")
    assert row is not None, "自动签到没有入库"
    assert int(row["total_checkins"]) == 1
    # 100 基础 + 0~50 随机 + 200 首签礼
    assert 300 <= int(row["coins"]) <= 350
    assert row["last_checkin_date"] is not None
    assert len(replies) == 1
    assert replies[0].startswith("签到成功，+")
    assert "连续 1 天" in replies[0]
    assert event.replies == replies
    # 自动签到不该吞掉普通聊天
    assert event.stopped is False

    await plugin.terminate()


async def test_auto_checkin_is_idempotent_within_a_day(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    first = [item async for item in plugin.auto_check_in(GroupEvent(message="早上好"))]
    second = [item async for item in plugin.auto_check_in(GroupEvent(message="中午好"))]

    assert len(first) == 1
    assert second == []
    row = await plugin.database.fetchone("SELECT total_checkins, coins FROM players")
    assert int(row["total_checkins"]) == 1
    assert 300 <= int(row["coins"]) <= 350

    await plugin.terminate()


async def test_auto_checkin_stays_silent_outside_the_whitelist(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["999"]}},
    )
    await plugin.initialize()

    replies = [item async for item in plugin.auto_check_in(GroupEvent(message=""))]

    assert replies == []
    row = await plugin.database.fetchone("SELECT COUNT(*) AS total FROM players")
    assert int(row["total"]) == 0

    await plugin.terminate()


async def test_auto_checkin_cycle_mode_speaks_only_on_cycle_days(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={
            "access": {"group_whitelist": ["100"]},
            "checkin": {"auto_checkin_reply": "cycle"},
        },
    )
    await plugin.initialize()

    # 第 1 天：安静
    assert [item async for item in plugin.auto_check_in(GroupEvent(message="第一天"))] == []

    # 另造一个"昨天已经连签 6 天"的玩家：他今天的第一条消息就是第 7 天（命中周期）
    yesterday = (
        datetime.now(plugin.service.zone).date() - timedelta(days=1)
    ).isoformat()
    stamp = "2026-01-01T00:00:00+00:00"
    await plugin.database.execute(
        "INSERT INTO players (platform, group_id, user_id, nickname, level, coins,"
        " total_coins, total_checkins, streak_days, best_streak, last_checkin_date,"
        " created_at, updated_at) VALUES ('aiocqhttp', '100', 'zeta', '龟丁', 1, 0,"
        " 0, 6, 6, 6, ?, ?, ?)",
        (yesterday, stamp, stamp),
    )
    await plugin.database.connection.commit()

    replies = [
        item
        async for item in plugin.auto_check_in(GroupEvent(message="第七天", user_id="zeta"))
    ]
    assert len(replies) == 1
    assert "连签 7 天，额外 +300 币" in replies[0]

    row = await plugin.database.fetchone(
        "SELECT streak_days, best_streak FROM players WHERE user_id = 'zeta'",
    )
    assert int(row["streak_days"]) == 7
    assert int(row["best_streak"]) == 7

    await plugin.terminate()


async def test_auto_checkin_silent_mode_never_replies(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={
            "access": {"group_whitelist": ["100"]},
            "checkin": {"auto_checkin_reply": "silent"},
        },
    )
    await plugin.initialize()

    replies = [item async for item in plugin.auto_check_in(GroupEvent(message=""))]
    assert replies == []
    row = await plugin.database.fetchone("SELECT total_checkins FROM players")
    assert int(row["total_checkins"]) == 1  # 静默但仍然入库

    await plugin.terminate()


async def test_auto_checkin_never_speaks_on_woken_messages(plugin_module):
    """被 @ 的消息多半自带回复，自动签到不抢话，但签到照常入库。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    replies = [
        item
        async for item in plugin.auto_check_in(GroupEvent(message="你好", woken=True))
    ]
    assert replies == []
    row = await plugin.database.fetchone("SELECT total_checkins FROM players")
    assert int(row["total_checkins"]) == 1

    await plugin.terminate()


async def test_auto_checkin_ignores_our_own_commands(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    # 唤醒前缀已被 AstrBot 剥离，所以指令事件里看到的是「签到」
    replies = [
        item
        async for item in plugin.auto_check_in(GroupEvent(message="签到", woken=True))
    ]
    assert replies == []
    row = await plugin.database.fetchone("SELECT COUNT(*) AS total FROM players")
    assert int(row["total"]) == 0  # 由指令 handler 负责，自动签到不插手

    await plugin.terminate()


# ---------------------------------------------------------------------------
# 决斗
# ---------------------------------------------------------------------------


class FakeOneBot:
    """假的 OneBot 客户端：机器人自己是管理员，其他成员默认普通成员。

    ``roles`` 可以按 user_id 覆盖某个成员的角色（用于测"管理员败者"）。
    """

    def __init__(
        self,
        *,
        role: str = "admin",
        self_id: str = "1",
        roles: dict[str, str] | None = None,
    ) -> None:
        self.role = role
        self.self_id = self_id
        self.roles = dict(roles or {})
        self.actions: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **params):
        self.actions.append((action, params))
        if action == "get_group_member_info":
            user_id = str(params.get("user_id"))
            role = self.role if user_id == self.self_id else self.roles.get(user_id, "member")
            return {"role": role, "shut_up_timestamp": 0}
        return None


def duel_event(
    *,
    group_id: str = "100",
    user_id: str = "200",
    sender_name: str = "龟甲",
    target: str = "300",
    bot=None,
) -> GroupEvent:
    event = GroupEvent(
        message=f"决斗 @龟乙({target})",
        group_id=group_id,
        user_id=user_id,
        sender_name=sender_name,
        woken=True,
    )
    event.bot = bot if bot is not None else FakeOneBot()
    event.get_platform_name = lambda: "aiocqhttp"  # type: ignore[method-assign]
    event.get_messages = lambda: [FakeAt(target, "龟乙")]  # type: ignore[method-assign]
    return event


def plugin_identity(user_id: str, *, group_id: str = "100"):
    """用插件自己那份 funhub 包里的身份类型（避免与测试进程里的副本混用）。"""
    from funhub_plugin_under_test.funhub.players import PlayerIdentity

    return PlayerIdentity(
        platform="aiocqhttp",
        group_id=group_id,
        user_id=user_id,
        nickname=user_id,
    )


async def seed_coins(plugin, user_id: str, coins: int) -> None:
    from funhub_plugin_under_test.funhub.players import PlayerRepository

    repository = PlayerRepository(plugin.database)
    async with plugin.database.transaction() as connection:
        player, _ = await repository.get_or_create_in_db(
            connection,
            plugin_identity(user_id),
        )
        await repository.apply_coin_delta_in_db(
            connection,
            player_pk=player.id,
            delta=coins,
            reason="seed",
        )


class SequenceRng:
    """按顺序吐出预设值的随机源，让决斗结果可控。"""

    def __init__(self, *values: int) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0) if self.values else low
        return min(max(value, low), high)


async def test_duel_command_mutes_a_losing_challenger(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, "200", 5000)
    await seed_coins(plugin, "300", 5000)

    # 挑战者(200) 掷 10、对手(300) 掷 90 → 挑事的输了
    plugin.duel_service.rng = SequenceRng(10, 90, 200)
    bot = FakeOneBot()
    replies = [item async for item in plugin.duel(duel_event(bot=bot), "")]

    assert len(replies) == 1
    lines = replies[0].splitlines()
    assert lines[0] == "决斗 · 龟甲 vs 龟乙"
    assert lines[1] == "10 : 90"
    assert lines[2] == "龟乙 胜，赢得 200 龟龟币"
    assert lines[3] == "龟甲 被禁言 1 分钟"

    actions = dict(bot.actions)
    assert actions["get_group_member_info"]["user_id"] == 1  # 查的是机器人自己
    assert actions["set_group_ban"]["user_id"] == 200  # 禁的是挑事的那个人
    assert actions["set_group_ban"]["duration"] == 60

    await plugin.terminate()


async def test_duel_command_boosts_an_admin_loser(plugin_module):
    """败者是群管理员：平台禁不了他，卡片按 200 × 1.2 展示。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, "200", 5000)
    await seed_coins(plugin, "300", 5000)

    plugin.duel_service.rng = SequenceRng(90, 10, 200)  # 挑战者赢
    bot = FakeOneBot(roles={"300": "owner"})  # 被打的那位是群主
    replies = [item async for item in plugin.duel(duel_event(bot=bot), "")]

    lines = replies[0].splitlines()
    assert lines[2] == "龟甲 胜，赢得 240 龟龟币"  # 200 × (1.0 + 0.2)
    assert lines[3] == "龟乙 是管理员，赔付 ×1.2"
    assert "set_group_ban" not in dict(bot.actions)  # 明知禁不了就不调接口

    await plugin.terminate()


async def test_duel_command_boosts_a_rich_loser(plugin_module):
    """存款碾压：败者比胜者多 2000 → 1.4 倍。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, "200", 500)  # 胜者
    await seed_coins(plugin, "300", 2500)  # 败者，多 2000

    plugin.duel_service.rng = SequenceRng(90, 10, 200)  # 挑战者赢
    replies = [item async for item in plugin.duel(duel_event(bot=FakeOneBot()), "")]

    lines = replies[0].splitlines()
    assert lines[2] == "龟甲 胜，赢得 280 龟龟币"  # 200 × 1.4
    assert lines[3] == "龟乙 存款多 2000，赔付 ×1.4"

    await plugin.terminate()


async def test_duel_command_does_not_mute_a_losing_target(plugin_module):
    """被打的人输了：只赔币，不会被禁言。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, "200", 5000)
    await seed_coins(plugin, "300", 5000)

    plugin.duel_service.rng = SequenceRng(90, 10, 200)  # 挑战者赢
    bot = FakeOneBot()
    replies = [item async for item in plugin.duel(duel_event(bot=bot), "")]

    lines = replies[0].splitlines()
    assert lines[1] == "90 : 10"
    assert lines[2] == "龟甲 胜，赢得 200 龟龟币"
    assert len(lines) == 3  # 没有禁言那一行
    assert "set_group_ban" not in dict(bot.actions)

    await plugin.terminate()


async def test_duel_command_without_a_target_prints_usage(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = GroupEvent(message="决斗", woken=True)
    event.bot = FakeOneBot()
    event.get_platform_name = lambda: "aiocqhttp"  # type: ignore[method-assign]
    replies = [item async for item in plugin.duel(event, "")]

    assert replies == ["用法：/决斗 @某人"]
    assert event.stopped is True
    rows = await plugin.database.fetchone("SELECT COUNT(*) AS total FROM players")
    assert int(rows["total"]) == 0

    await plugin.terminate()


async def test_duel_command_is_disabled_by_config(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={
            "access": {"group_whitelist": ["100"]},
            "duel": {"enabled": False},
        },
    )
    await plugin.initialize()

    replies = [item async for item in plugin.duel(duel_event(), "")]
    assert replies == ["决斗没开"]

    await plugin.terminate()


async def test_duel_command_is_blocked_outside_the_whitelist(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["999"]}},
    )
    await plugin.initialize()

    event = duel_event()
    replies = [item async for item in plugin.duel(event, "")]

    assert replies == []
    assert event.stopped is False
    rows = await plugin.database.fetchone("SELECT COUNT(*) AS total FROM players")
    assert int(rows["total"]) == 0

    await plugin.terminate()


# ---------------------------------------------------------------------------
# 轮盘
# ---------------------------------------------------------------------------


class RoundRng:
    """轮盘用随机源：弹槽固定，``random()`` 按顺序取，用完给 0.99（子弹那一枪不炸膛）。"""

    def __init__(self, chamber: int, *random_values: float) -> None:
        self.chamber = chamber
        self.values = list(random_values)

    def randint(self, low: int, high: int) -> int:
        return min(max(self.chamber, low), high)

    def random(self) -> float:
        return self.values.pop(0) if self.values else 0.99


def roulette_event(
    *,
    message: str = "轮盘",
    user_id: str = "200",
    sender_name: str = "龟甲",
    target: str | None = None,
    bot=None,
) -> GroupEvent:
    event = GroupEvent(message=message, user_id=user_id, sender_name=sender_name, woken=True)
    event.bot = bot if bot is not None else FakeOneBot()
    event.get_platform_name = lambda: "aiocqhttp"  # type: ignore[method-assign]
    if target is not None:
        event.get_messages = lambda: [FakeAt(target, "龟乙")]  # type: ignore[method-assign]
    return event


async def test_roulette_flows_through_the_commands(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, "300", 1000)
    plugin.roulette_service.rng = RoundRng(2)  # 第 2 枪响

    started = [item async for item in plugin.roulette(roulette_event())]
    lines = started[0].splitlines()
    assert lines[0] == "轮盘装填完毕（6 个弹槽 · 1 颗子弹）"
    assert lines[1] == "空枪 +50 币，之后每枪再多 50"
    assert lines[2] == "子弹那一枪 25% 概率炸膛，开枪的人白拿 500 币"
    assert lines[3] == "中弹扣 500 币，全赔给开过枪的人，禁言 1 分钟"

    busy = [item async for item in plugin.roulette(roulette_event(user_id="999"))]
    assert busy == ["本群已经有一局轮盘了"]

    miss = [item async for item in plugin.shoot(roulette_event(message="开枪"))]
    assert miss == ["空枪（1 / 6），+50 币"]

    hit = [
        item
        async for item in plugin.shoot(
            roulette_event(message="开枪", user_id="300", sender_name="龟乙"),
        )
    ]
    lines = hit[0].splitlines()
    assert lines[0] == "砰，龟乙 中弹（2 / 6）"
    assert lines[1] == "扣 500 币，禁言 1 分钟"
    assert lines[2] == "龟甲 分到 500 币"  # 写名字，不说"其他 1 人"

    assert [item async for item in plugin.shoot(roulette_event(message="开枪"))] == [
        "现在没有进行中的轮盘",
    ]

    await plugin.terminate()


async def test_roulette_misfire_flows_through_the_commands(plugin_module):
    """子弹那一枪炸膛：走完整条指令链，卡片给钱、不罚不踢。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    plugin.roulette_service.rng = RoundRng(6, 0.05)  # 第 6 枪才响，但掷到炸膛

    await anext(plugin.roulette(roulette_event()), None)  # 开局
    for index in range(5):
        await anext(
            plugin.shoot(roulette_event(message="开枪", user_id=f"30{index}")),
            None,
        )  # 前 5 枪空枪，第 6 枪才轮到子弹
    last = [
        item
        async for item in plugin.shoot(
            roulette_event(message="开枪", user_id="200", sender_name="龟甲"),
        )
    ]

    assert last == ["炸膛（6 / 6），龟甲 +500 币"]
    row = await plugin.database.fetchone(
        "SELECT coins FROM players WHERE user_id = ?",
        ("200",),
    )
    assert int(row["coins"]) == 500

    await plugin.terminate()


async def test_roulette_is_disabled_by_config(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={
            "access": {"group_whitelist": ["100"]},
            "roulette": {"enabled": False},
        },
    )
    await plugin.initialize()

    assert [item async for item in plugin.roulette(roulette_event())] == ["轮盘没开"]

    await plugin.terminate()


async def test_rescue_needs_a_target(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    replies = [item async for item in plugin.rescue(roulette_event(message="救一下"), "")]
    assert replies == ["用法：/救一下 @某人"]

    await plugin.terminate()


async def test_rescue_unbans_the_mentioned_member(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    plugin.roulette_service.rng = RoundRng(1, 0.9)  # 救援不失败

    bot = FakeOneBot()
    replies = [
        item
        async for item in plugin.rescue(
            roulette_event(message="救一下", target="300", bot=bot),
            "",
        )
    ]

    assert replies == ["龟乙 被捞出来了"]
    assert dict(bot.actions)["set_group_ban"]["duration"] == 0
    assert dict(bot.actions)["set_group_ban"]["user_id"] == 300

    await plugin.terminate()


async def test_judgment_extends_the_mute_through_the_command(plugin_module):
    """``/补一枪 @刚中弹的人``：走完整条指令链，追加一次禁言。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    plugin.roulette_service.rng = RoundRng(1)  # 第一枪就响，50% 炸膛那条默认不触发（0.99）

    bot = FakeOneBot()
    await anext(plugin.roulette(roulette_event()), None)
    await anext(
        plugin.shoot(roulette_event(message="开枪", user_id="300", sender_name="龟乙")),
        None,
    )  # 龟乙中弹被禁言，进入补枪名单
    plugin.roulette_service.rng = RoundRng(1, 0.9, 0.9)  # 不卡壳、不反噬

    replies = [
        item
        async for item in plugin.judgment(
            roulette_event(message="补一枪", target="300", bot=bot),
            "",
        )
    ]

    assert replies == ["补了一枪，龟乙 又被禁言 1 分钟"]
    assert dict(bot.actions)["set_group_ban"]["user_id"] == 300
    assert dict(bot.actions)["set_group_ban"]["duration"] == 60

    await plugin.terminate()


async def test_judgment_rejects_people_who_were_not_shot(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    plugin.roulette_service.rng = RoundRng(1, 0.9)

    replies = [
        item
        async for item in plugin.judgment(
            roulette_event(message="补一枪", target="300"),
            "",
        )
    ]

    assert replies == ["没有可以补枪的人（名单里的人都出来了）"]

    await plugin.terminate()


# ---------------------------------------------------------------------------
# @ 唤醒通道（QQ 官方机器人把 <@id> 留在文本里）
# ---------------------------------------------------------------------------

BOT_ID = "BE7AA4113AC560F0A026F17176580BDE"
MEMBER_ID = "BC729CFA021694764C24EF8C285DD78B"


class QqOfficialEvent(GroupEvent):
    """QQ 官方机器人的事件：文本里留着 <@机器人id>，At 组件不一定有。

    默认 ``woken=False`` —— 这个平台既不剥掉 @ 标记、也常常不给 ``At`` 组件，
    ``is_at_or_wake_command`` 因此一直是假，这正是需要兜底通道的原因。
    """

    def __init__(
        self,
        raw: str,
        *,
        self_id: str = BOT_ID,
        user_id: str = MEMBER_ID,
        messages: tuple = (),
        woken: bool = False,
    ) -> None:
        super().__init__(message=raw, user_id=user_id, sender_name="荃翁龟", woken=woken)
        self._self_id = self_id
        self._messages = list(messages)
        self.unified_msg_origin = f"qq_official:GroupMessage:{self._group_id}"

    def get_self_id(self):
        return self._self_id

    def get_platform_id(self):
        return "qq_official"

    def get_platform_name(self):
        return "qq_official"

    def get_messages(self):
        return list(self._messages)

    def get_message_str(self):
        return self._message


async def test_mention_markup_still_dispatches_the_duel_command(plugin_module):
    """用户报的场景：`<@机器人> 艾斯比 <@对手>` 在 QQ 官方机器人上必须能触发决斗。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, "TARGET", 5000)

    event = QqOfficialEvent(
        f"<@{BOT_ID}> 艾斯比 <@TARGET>",
        messages=(FakeAt(BOT_ID), FakeAt("TARGET", "龟乙")),
    )
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    # @机器人 在前也不能把他当对手
    assert replies[0].splitlines()[0] == "决斗 · 荃翁龟 vs 龟乙"
    assert event.stopped is True

    await plugin.terminate()


async def test_mention_markup_dispatches_check_in(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = QqOfficialEvent(f"<@{BOT_ID}> 签到")
    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].startswith("签到成功，+")
    row = await plugin.database.fetchone("SELECT total_checkins FROM players")
    assert int(row["total_checkins"]) == 1

    await plugin.terminate()


# ---------------------------------------------------------------------------
# 从老插件 tests/test_command_handler.py 的 QQ 官方兼容用例照搬过来的场景
# （AstrBotMentionCompatibilityTests）
# ---------------------------------------------------------------------------


async def test_legacy_case_markup_target_before_wake_word(plugin_module):
    """老插件用例 1：``<qqbot-at-user id="对手"/> 艾斯比 …`` 应当开打。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, "target-openid", 5000)

    event = QqOfficialEvent(
        '<qqbot-at-user id="target-openid" /> 艾斯比 防守反击',
        messages=(FakeAt("qq_official", "机器人"),),
    )
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].splitlines()[0] == "决斗 · 荃翁龟 vs target-openid"

    await plugin.terminate()


async def test_legacy_case_target_at_misreported_as_self_id(plugin_module):
    """老插件用例 2：``self_id`` 其实是**对手**的 openid 时，仍要以他为对手。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    target_id = "B2DDC6FFD2F562C68CC02CAD749EF622"
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, target_id, 5000)

    event = QqOfficialEvent("艾斯比", self_id=target_id, messages=(FakeAt(target_id, ""),))
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].splitlines()[0] == f"决斗 · 荃翁龟 vs {target_id}"

    await plugin.terminate()


async def test_legacy_case_bot_only_mention_is_not_a_target(plugin_module):
    """老插件用例 3：非官方平台只 @ 了机器人时，没有对手，不该开打。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = GroupEvent(message="艾斯比", user_id="200", woken=True)
    event.get_self_id = lambda: "bot-1"  # type: ignore[method-assign]
    event.get_messages = lambda: [FakeAt("bot-1", "机器人")]  # type: ignore[method-assign]
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert replies == ["用法：/决斗 @某人"]

    await plugin.terminate()


async def test_legacy_case_bot_markup_then_command_then_target(plugin_module):
    """老插件用例 4：``<qqbot-at-user id="qq_official"/> 挑战 <对手>``。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, "target-openid", 5000)

    event = QqOfficialEvent(
        '<qqbot-at-user id="qq_official" /> 挑战 '
        '<qqbot-at-user id="target-openid" /> 游走消耗',
        messages=(FakeAt("qq_official", "机器人"),),
    )
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].splitlines()[0] == "决斗 · 荃翁龟 vs target-openid"

    await plugin.terminate()


async def test_mention_dispatch_ignores_plain_chat(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    # 文本里带 @，但说的不是指令
    event = QqOfficialEvent(f"<@{BOT_ID}> 今天天气不错")
    assert [item async for item in plugin.mentioned_command(event)] == []

    # 完全没叫机器人的裸指令：仍然不处理（必须走唤醒）
    idle = GroupEvent(message="签到", woken=False)
    assert [item async for item in plugin.mentioned_command(idle)] == []
    rows = await plugin.database.fetchone("SELECT COUNT(*) AS total FROM players")
    assert int(rows["total"]) == 0

    await plugin.terminate()


async def test_mention_dispatch_works_without_the_wake_flag(plugin_module):
    """QQ 官方机器人既不给 At 组件、也不置 is_at_or_wake_command，仍要能唤醒。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = QqOfficialEvent(f"<@{BOT_ID}> 签到")
    assert event.is_at_or_wake_command is False  # 平台没给唤醒标记
    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].startswith("签到成功，+")
    row = await plugin.database.fetchone("SELECT total_checkins FROM players")
    assert int(row["total_checkins"]) == 1

    await plugin.terminate()


async def test_decoration_prefix_still_hits_the_command(plugin_module):
    """玩家顺手把插件名带上：``/龟龟轮盘`` 也算 ``/轮盘``。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    # AstrBot 唤醒时已经剥掉前缀，留下的就是「龟龟轮盘」
    event = GroupEvent(message="龟龟轮盘", user_id="200", woken=True)
    replies = [item async for item in plugin.mentioned_command(event)]

    assert replies[0].splitlines()[0] == "轮盘装填完毕（6 个弹槽 · 1 颗子弹）"

    # @ 通道同理
    mentioned = QqOfficialEvent(f"<@{BOT_ID}> 龟龟签到")
    assert [item async for item in plugin.mentioned_command(mentioned)][0].startswith("签到成功，+")

    row = await plugin.database.fetchone("SELECT COUNT(*) AS total FROM checkins")
    assert int(row["total"]) == 1

    await plugin.terminate()


async def test_misspelled_command_gets_a_hint_instead_of_silence(plugin_module):
    """打错指令名不能一声不吭 —— 那看起来就是"插件没反应"。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = GroupEvent(message="轮盘啦", user_id="200", woken=True)
    assert [item async for item in plugin.mentioned_command(event)] == ["指令是 /轮盘"]
    assert event.stopped is True

    # 只在"本来就想输入指令"时提示：闲聊里提到指令名不打扰
    chat = GroupEvent(message="今天这个面板挺好看", user_id="200", woken=True)
    assert [item async for item in plugin.mentioned_command(chat)] == []

    # 指令名写对时不会多这一句
    right = GroupEvent(message="轮盘", user_id="200", woken=True)
    assert [item async for item in plugin.mentioned_command(right)][0].startswith("轮盘装填完毕")

    await plugin.terminate()


async def test_typo_of_an_alias_hints_the_canonical_command(plugin_module):
    """别名打错时提示主名，别让玩家去猜。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = GroupEvent(message="排行榜啦", user_id="200", woken=True)
    assert [item async for item in plugin.mentioned_command(event)] == ["指令是 /排行"]

    await plugin.terminate()


async def test_hint_is_silent_outside_the_whitelist(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    outside = GroupEvent(message="轮盘啦", group_id="999", user_id="200", woken=True)
    assert [item async for item in plugin.mentioned_command(outside)] == []

    await plugin.terminate()


async def test_auto_checkin_keeps_quiet_when_a_hint_is_coming(plugin_module):
    """打错指令的那条已经会有提示，别再插一张签到卡。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={
            "access": {"group_whitelist": ["100"]},
            "checkin": {"auto_checkin": True, "auto_checkin_reply": "always"},
        },
    )
    await plugin.initialize()

    typo = GroupEvent(message="轮盘啦", user_id="200", woken=True)
    assert [item async for item in plugin.auto_check_in(typo)] == []

    # 普通聊天照常自动签到
    chat = GroupEvent(message="下午好", user_id="200", woken=False)
    assert [item async for item in plugin.auto_check_in(chat)][0].startswith("签到成功，+")

    await plugin.terminate()


async def test_legacy_config_defaults_are_upgraded_once(plugin_module):
    """旧版本存下来的默认值不会自己跟着新默认值走，插件要自己升一次。"""
    module, _, _ = plugin_module
    legacy = {
        "access": {"group_whitelist": ["100"]},
        "roulette": {
            "safe_reward": 100,
            "share_percent": 50,
            "mute_seconds": 600,
            "misfire_percent": 12.5,
        },
        "duel": {"admin_percent": 50},
    }
    plugin = module.FunHubPlugin(context=module.Context(), config=legacy)
    await plugin.initialize()

    assert plugin.settings.roulette.safe_reward == 50
    assert plugin.settings.roulette.share_percent == 100
    assert plugin.settings.roulette.mute_seconds == 60
    assert plugin.settings.roulette.misfire_percent == 25
    assert plugin.settings.duel.admin_percent == 20
    # 服务层必须一起换掉，否则玩法还是老数值
    assert plugin.roulette_service.settings.roulette.safe_reward == 50
    assert plugin.duel_service.settings.duel.admin_percent == 20
    assert await plugin.database.get_meta(module.CONFIG_META_KEY) == str(
        module.CONFIG_SCHEMA_VERSION,
    )

    await plugin.terminate()


async def test_drunk_state_is_read_from_the_repeater_plugin(plugin_module):
    """跨插件读醉酒：插件表里有 repeater 就能读到它的内存状态。"""
    module, _, _ = plugin_module

    class FakeEngine:
        def drunk_remaining(self, group_key):
            assert group_key == "aiocqhttp:100"
            return 300

    star = types.SimpleNamespace(
        root_dir_name="astrbot_plugin_repeater",
        name="astrbot_plugin_repeater",
        module_path="data.plugins.astrbot_plugin_repeater.main",
        star_cls=types.SimpleNamespace(engine=FakeEngine()),
        activated=True,
    )
    plugin = module.FunHubPlugin(
        context=module.Context(stars=[star]),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    state = await plugin.roulette_service.drunk_state(plugin_identity("a"))
    assert state.drunk is True
    assert state.seconds == 300
    assert state.source == "astrbot_plugin_repeater"

    await plugin.terminate()


async def test_startup_reports_whether_the_drunk_link_is_up(plugin_module, monkeypatch):
    """醉酒联动接没接上必须一眼可见（不然"补一枪怎么不反噬"没人答得上来）。"""
    module, _, _ = plugin_module
    lines: list[str] = []
    for level in ("info", "warning"):
        monkeypatch.setattr(
            module.logger,
            level,
            lambda message, *a, **k: lines.append(str(message)),
            raising=False,
        )

    without = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await without.initialize()
    assert any("没找到喝酒插件" in line for line in lines)
    await without.terminate()

    lines.clear()
    star = types.SimpleNamespace(
        root_dir_name="astrbot_plugin_repeater",
        name="astrbot_plugin_repeater",
        module_path="data.plugins.astrbot_plugin_repeater.main",
        star_cls=types.SimpleNamespace(engine=None),
        activated=True,
    )
    with_repeater = module.FunHubPlugin(
        context=module.Context(stars=[star]),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await with_repeater.initialize()
    assert any("醉酒状态接的是 astrbot_plugin_repeater" in line for line in lines)
    await with_repeater.terminate()


async def test_upgrade_does_not_fight_the_user_afterwards(plugin_module):
    """升级只跑一次：用户之后自己把值改回 100，就得听用户的。"""
    module, _, _ = plugin_module
    first = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}, "roulette": {"safe_reward": 100}},
    )
    await first.initialize()
    assert first.settings.roulette.safe_reward == 50
    await first.terminate()

    again = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}, "roulette": {"safe_reward": 100}},
    )
    await again.initialize()

    assert again.settings.roulette.safe_reward == 100

    await again.terminate()


async def test_user_tuned_values_survive_the_upgrade(plugin_module, monkeypatch):
    """只升"还停在旧默认值"的项，自己调过的数值不动。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={
            "access": {"group_whitelist": ["100"]},
            "roulette": {"safe_reward": 250, "mute_seconds": 600},
        },
    )
    await plugin.initialize()

    assert plugin.settings.roulette.safe_reward == 250  # 用户调的，保留
    assert plugin.settings.roulette.mute_seconds == 60  # 旧默认值，升级

    await plugin.terminate()


async def test_whitelist_rejection_is_logged_once(plugin_module, monkeypatch):
    """白名单外的指令静默，但必须留痕，否则看起来像插件坏了。"""
    module, _, _ = plugin_module
    logged: list[str] = []
    monkeypatch.setattr(
        module.logger,
        "warning",
        lambda message, *a, **k: logged.append(str(message)),
        raising=False,
    )
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["999"]}},
    )
    await plugin.initialize()

    event = QqOfficialEvent(f"<@{BOT_ID}> 签到")
    assert [item async for item in plugin.mentioned_command(event)] == []
    assert any("不在白名单" in message for message in logged)
    assert sum("不在白名单" in message for message in logged) == 1

    # 同一个群再来一次不再重复提示
    assert [item async for item in plugin.mentioned_command(event)] == []
    assert sum("不在白名单" in message for message in logged) == 1

    await plugin.terminate()


async def test_empty_whitelist_warns_at_startup(plugin_module, monkeypatch):
    """白名单为空 = 任何群都不响应，这条必须显眼（"插件没反应"的头号原因）。"""
    module, _, _ = plugin_module
    warned: list[str] = []
    monkeypatch.setattr(
        module.logger,
        "warning",
        lambda message, *a, **k: warned.append(str(message)),
        raising=False,
    )
    plugin = module.FunHubPlugin(context=module.Context(), config={})
    await plugin.initialize()

    assert any("白名单为空" in message for message in warned)

    await plugin.terminate()


async def test_command_is_handled_only_once_per_message(plugin_module):
    """两条路径都可能命中，但不能回两次。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    # aiocqhttp 的形态：@ 已被平台剥掉，CommandFilter 与兜底通道都能匹配
    event = GroupEvent(message="签到", woken=True)
    first = [item async for item in plugin.check_in(event)]
    second = [item async for item in plugin.mentioned_command(event)]

    assert len(first) == 1
    assert second == []
    row = await plugin.database.fetchone("SELECT total_checkins FROM players")
    assert int(row["total_checkins"]) == 1

    await plugin.terminate()


# ---------------------------------------------------------------------------
# 唤起词通配：出现在句子任何位置都算
# ---------------------------------------------------------------------------


async def test_wake_word_inside_a_sentence_still_triggers(plugin_module):
    """「艾斯比吧你」这种带后缀的说法也要能开打（老插件就是句中匹配）。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, "TARGET", 5000)

    event = QqOfficialEvent(
        f"<@{BOT_ID}> 你这个艾斯比吧你 <@TARGET>",
        messages=(FakeAt(BOT_ID), FakeAt("TARGET", "龟乙")),
    )
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].splitlines()[0] == "决斗 · 荃翁龟 vs 龟乙"

    await plugin.terminate()


async def test_lone_mention_is_taken_as_the_opponent(plugin_module):
    """QQ 官方机器人上只有唯一一个 @ 时，只能当他就是对手。

    平台把对手的 openid 当 ``self_id`` 上报过（老插件为此写了用例），所以无法区分
    "只 @ 了机器人"和"只 @ 了对手"。老插件的选择是"算对手"，这里保持一致：
    ``<@机器人> 你个啥比`` 会跟那个账号开打；代价是可能打到自己头上，但不会出错。
    """
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, BOT_ID, 5000)

    event = QqOfficialEvent(f"<@{BOT_ID}> 你个啥比")
    event.bot = FakeOneBot()
    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    assert replies[0].splitlines()[0] == f"决斗 · 荃翁龟 vs {BOT_ID}"

    await plugin.terminate()


async def test_placeholder_mention_is_not_a_target(plugin_module):
    """占位 id（qq_official）明确代表机器人，不会被当对手。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = QqOfficialEvent("<@qq_official> 你个啥比")
    replies = [item async for item in plugin.mentioned_command(event)]

    assert replies == ["用法：/决斗 @某人"]

    await plugin.terminate()


async def test_wildcard_chatter_does_not_eat_the_daily_checkin(plugin_module):
    """自动签到的跳过判定要和派发一致：光说「挑战」不会白白吃掉当天签到。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    # 没有对手：既不会派发决斗，也不该拦下自动签到
    chatter = QqOfficialEvent(f"<@{BOT_ID}> 这个挑战真难")
    assert plugin._dispatchable_command(chatter) is None
    replies = [item async for item in plugin.auto_check_in(chatter)]
    assert len(replies) == 1
    assert replies[0].startswith("签到成功，+")

    await plugin.terminate()


async def test_wildcard_duel_message_skips_the_checkin_card(plugin_module):
    """确实要开打的消息：不该再插一张签到卡。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()

    event = QqOfficialEvent(
        f"<@{BOT_ID}> 艾斯比吧你 <@TARGET>",
        messages=(FakeAt(BOT_ID), FakeAt("TARGET", "龟乙")),
    )
    assert plugin._dispatchable_command(event) is not None
    assert [item async for item in plugin.auto_check_in(event)] == []

    await plugin.terminate()


async def test_llm_guard_stays_strict_about_wildcards(plugin_module, monkeypatch):
    """白名单外的闲聊里出现唤起词，不该被插件吞掉 LLM 回复（守卫只认前缀形式）。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["999"]}},
    )
    await plugin.initialize()

    chatter = QqOfficialEvent(f"<@{BOT_ID}> 你个艾斯比")
    await plugin.block_commands_outside_whitelist(chatter)
    assert chatter.stopped is False

    # 前缀形式的指令仍然会被拦下
    command = QqOfficialEvent(f"<@{BOT_ID}> 签到")
    await plugin.block_commands_outside_whitelist(command)
    assert command.stopped is True

    await plugin.terminate()


async def test_mention_target_can_come_from_text_markup(plugin_module):
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, MEMBER_ID, 5000)
    await seed_coins(plugin, "TARGET", 5000)

    event = QqOfficialEvent(f"<@{BOT_ID}> 艾斯比 <@{'TARGET'}>", messages=())
    event.bot = FakeOneBot()

    replies = [item async for item in plugin.mentioned_command(event)]

    assert len(replies) == 1
    lines = replies[0].splitlines()
    # 没有 At 组件，拿不到群名片，就用 id 当展示名；@机器人 在前也不能当对手
    assert lines[0] == "决斗 · 荃翁龟 vs TARGET"

    await plugin.terminate()


async def test_plain_duel_command_still_needs_a_wake(plugin_module):
    """``决斗`` 不是唤起词：没唤醒机器人时不动手（只有 艾斯比 / 啥比 免唤醒）。"""
    module, _, _ = plugin_module
    plugin = module.FunHubPlugin(
        context=module.Context(),
        config={"access": {"group_whitelist": ["100"]}},
    )
    await plugin.initialize()
    await seed_coins(plugin, "TARGET", 5000)

    event = QqOfficialEvent(f"<@{BOT_ID}> 决斗 <@{'TARGET'}>", messages=())
    assert [item async for item in plugin.mentioned_command(event)] == []

    await plugin.terminate()
