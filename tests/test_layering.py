"""分层约束：业务包不依赖 astrbot，指令必须走唤醒前缀，Pages 存在。

这些断言是"设计约束"的守卫：``funhub/`` 一旦 import astrbot，脱离宿主就再也测不了；
指令一旦换回 regex 过滤器，就会绕过 AstrBot 的唤醒机制。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNHUB = ROOT / "funhub"
SCRIPTS = ROOT / "scripts"
MAIN = ROOT / "main.py"
COMMANDS = FUNHUB / "commands.py"
PAGE = ROOT / "pages" / "import"


def iter_python_files(directory: Path):
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_business_package_never_imports_astrbot():
    offenders: list[str] = []
    for path in [*iter_python_files(FUNHUB), *iter_python_files(SCRIPTS)]:
        for module in imported_modules(path):
            if module == "astrbot" or module.startswith("astrbot."):
                offenders.append(f"{path.relative_to(ROOT)} → {module}")
    assert offenders == [], offenders


def test_main_is_the_only_bridge():
    modules = imported_modules(MAIN)
    assert any(module.startswith("astrbot") for module in modules)


def test_commands_require_the_wake_prefix():
    text = MAIN.read_text(encoding="utf-8")
    names = set(re.findall(r'@filter\.command\(\s*"([^"]+)"', text))
    assert names == {"签到", "面板", "排行", "决斗", "轮盘", "开枪", "救一下", "补一枪"}
    # regex 过滤器不受唤醒前缀约束，本插件刻意不用
    assert "@filter.regex" not in text
    # 别名用 filter.command 的 alias 参数注册
    assert text.count("alias=COMMAND_ALIASES[") == len(names)


def test_duel_keeps_the_old_convenience_aliases():
    """老插件的挑战唤起词要原样保留（词表在 funhub/commands.py）。"""
    text = COMMANDS.read_text(encoding="utf-8")
    match = re.search(r'"决斗":\s*\{([^}]*)\}', text)
    assert match, "找不到决斗的别名定义"
    aliases = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert aliases == {"挑战", "艾斯比", "啥比"}
    wake = re.search(r"DUEL_WAKE_WORDS = frozenset\(\{([^}]*)\}\)", text)
    assert wake, "找不到挑战唤起词定义"
    assert set(re.findall(r'"([^"]+)"', wake.group(1))) == {"艾斯比", "啥比"}


def test_every_command_checks_the_whitelist():
    """每个指令入口 + @ 唤醒通道（含打错指令的提示）都先查白名单。"""
    text = MAIN.read_text(encoding="utf-8")
    assert text.count("_whitelist_ok(event)") == 10  # 8 个指令 + @ 唤醒通道 + 提示
    assert text.count("@whitelist_only") == 1


def test_ranking_uses_greedy_str_for_arguments():
    text = MAIN.read_text(encoding="utf-8")
    assert re.search(r"async def ranking\(self, event: AstrMessageEvent, args: GreedyStr\)", text)


def test_mention_dispatch_is_registered():
    """QQ 官方机器人会把 <@id> 留在文本里，必须有一条按去 @ 文本匹配的兜底通道。"""
    text = MAIN.read_text(encoding="utf-8")
    assert "@filter.event_message_type(filter.EventMessageType.ALL, priority=90)" in text
    assert "parse_command(event, ALL_COMMAND_NAMES, wildcard=DUEL_WILDCARD_NAMES)" in text
    # 自动签到的跳过判定必须和实际派发用同一套判断
    assert text.count("self._dispatchable_command(event)") == 2
    # 打错指令名时要给提示，并且自动签到也得让路（否则一条消息两个回复）
    assert text.count("near_miss_command(event, ALL_COMMAND_NAMES)") == 2


def test_llm_guard_is_registered():
    text = MAIN.read_text(encoding="utf-8")
    assert "@filter.on_waiting_llm_request(priority=1000)" in text


def test_import_page_is_discoverable_and_uses_the_bridge():
    assert (PAGE / "index.html").is_file()
    assert (PAGE / "app.js").is_file()
    assert (PAGE / "style.css").is_file()

    html = (PAGE / "index.html").read_text(encoding="utf-8")
    assert "./app.js" in html
    assert 'type="module"' in html  # bridge SDK 注入之后才执行

    js = (PAGE / "app.js").read_text(encoding="utf-8")
    assert "window.AstrBotPluginPage" in js
    assert "bridge.ready()" in js
    assert "bridge.upload(" in js
    assert "bridge.apiPost(" in js
    assert "bridge.apiGet(" in js


def test_web_routes_are_prefixed_with_the_plugin_name():
    text = MAIN.read_text(encoding="utf-8")
    routes = set(re.findall(r'f"/\{PLUGIN_NAME\}/([^"]+)"', text))
    assert routes == {
        "import/list",
        "import/upload",
        "import/preview",
        "import/apply",
    }


def test_page_endpoints_match_the_registered_routes():
    js = (PAGE / "app.js").read_text(encoding="utf-8")
    for endpoint in ("import/list", "import/upload", "import/preview", "import/apply"):
        assert f'"{endpoint}"' in js, endpoint


def test_metadata_matches_the_plugin_name_and_schema():
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    # AstrBot 的 PLUGIN_METADATA_REQUIRED_FIELDS：name / desc / version / author
    for field in ("name", "desc", "version", "author"):
        match = re.search(rf"^{field}:\s*(\S.*)$", metadata, re.MULTILINE)
        assert match and match.group(1).strip(), field
    assert re.search(r"^name:\s*astrbot_plugin_funhub\s*$", metadata, re.MULTILINE)
    assert re.search(r"^display_name:\s*\S+", metadata, re.MULTILINE)
    assert re.search(r"^version:\s*v?\d+\.\d+\.\d+", metadata, re.MULTILINE)
    assert "astrbot_version" in metadata

    text = MAIN.read_text(encoding="utf-8")
    assert 'PLUGIN_NAME = "astrbot_plugin_funhub"' in text


def test_i18n_file_is_nested_json():
    payload = json.loads(
        (ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(encoding="utf-8"),
    )
    assert "import" in payload["pages"]
    assert payload["pages"]["import"]["title"]


def test_requirements_and_pytest_config_exist():
    assert "aiosqlite" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "asyncio_mode = auto" in (ROOT / "pytest.ini").read_text(encoding="utf-8")
