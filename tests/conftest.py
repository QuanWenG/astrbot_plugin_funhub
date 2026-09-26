"""测试夹具。

这些测试**不 import astrbot**：插件业务都在 ``funhub/`` 包里，事件只用鸭子类型的
假对象代替。这也是 ``test_layering.py`` 要守住的边界。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import sys
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from funhub.checkin import CheckinService  # noqa: E402
from funhub.config import load_settings  # noqa: E402
from funhub.db import Database  # noqa: E402
from funhub.players import PlayerIdentity  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TMP_ROOT = REPO_ROOT / ".pytest-tmp"


@pytest.fixture
def tmp_path(request) -> Path:
    """仓库内的临时目录。

    默认的 ``tmp_path`` 建在系统临时目录，某些受限环境（例如带文件沙箱的 CI）
    不允许在那里写；放到仓库里则处处可用，``.gitignore`` 已忽略该目录。
    """
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", request.node.name)[:80] or "test"
    path = TMP_ROOT / safe_name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


class FakeAt:
    """替代 astrbot 的 At 消息段。"""

    def __init__(self, qq: str, name: str = "") -> None:
        self.qq = qq
        self.name = name


class FixedRng:
    """可控随机源：让奖励总额在测试里可精确断言。"""

    def __init__(self, value: int = 0) -> None:
        self.value = value

    def randint(self, low: int, high: int) -> int:
        return min(max(self.value, low), high)


class FakeEvent:
    """只实现插件真正用到的那几个事件方法。"""

    def __init__(
        self,
        *,
        message: str = "签到",
        group_id: str = "100",
        user_id: str = "200",
        self_id: str = "1",
        platform_id: str = "aiocqhttp",
        platform_name: str = "aiocqhttp",
        unified_msg_origin: str | None = None,
        is_at_or_wake_command: bool = True,
        sender_name: str = "龟甲",
        messages: tuple[object, ...] = (),
    ) -> None:
        self._message = message
        self._group_id = group_id
        self._user_id = user_id
        self._self_id = self_id
        self._platform_id = platform_id
        self._platform_name = platform_name
        self._sender_name = sender_name
        self._messages = list(messages)
        self.is_at_or_wake_command = is_at_or_wake_command
        self.unified_msg_origin = (
            unified_msg_origin
            if unified_msg_origin is not None
            else f"{platform_id}:GroupMessage:{group_id}"
        )
        self.stopped = False
        self._extras: dict[str, object] = {}

    def get_message_str(self) -> str:
        return self._message

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._user_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_platform_name(self) -> str:
        return self._platform_name

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_session_id(self) -> str:
        return f"{self._group_id}:{self._user_id}"

    def get_messages(self) -> list[object]:
        return list(self._messages)

    def get_extra(self, key: str | None = None, default=None):
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    def stop_event(self) -> None:
        self.stopped = True


def identity(
    *,
    platform: str = "aiocqhttp",
    group_id: str = "100",
    user_id: str = "200",
    nickname: str = "龟甲",
) -> PlayerIdentity:
    return PlayerIdentity(
        platform=platform,
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
    )


@pytest.fixture
def settings():
    return load_settings({})


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "plugin_data"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest_asyncio.fixture
async def database(tmp_path: Path):
    db = Database(tmp_path / "funhub.db")
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def service(database: Database, settings):
    return CheckinService(database, settings, rng=FixedRng(0))


@pytest.fixture
def make_service(database: Database, settings):
    """按需构造可控随机源的服务实例。"""

    def factory(random_value: int = 0, *, custom_settings=None) -> CheckinService:
        return CheckinService(
            database,
            custom_settings or settings,
            rng=FixedRng(random_value),
        )

    return factory


@pytest.fixture
def legacy_payload() -> dict:
    """老插件导出样例（含坏记录、越界等级、缺字段、未知字段）。"""
    return json.loads((FIXTURES / "legacy_export_sample.json").read_text(encoding="utf-8"))
