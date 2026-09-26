"""跨插件读取"群里喝醉没有"。

龟龟轮盘的醉酒联动（``/补一枪`` 的反噬只在喝醉时触发）需要知道这件事，而"喝酒"是
另一个插件（默认 `astrbot_plugin_repeater`，见它的 ``RepeaterEngine._drunk_until``）
的功能。状态只在对方**内存**里：

    self._drunk_until: dict[str, int]        # group_key → 到期时间戳
    def drunk_remaining(group_key, *, now=None) -> int

所以这里用鸭子类型从 AstrBot 的插件表里把那个实例摸出来，调它的公开方法读状态：

* 状态是**按 ``平台:群``** 存的（对方也是这么算 key 的，与我们的
  :meth:`RouletteService.key_for` 同一个格式）；
* 对方没装 / 没加载 / 方法改名 / 抛异常 → 一律当作**清醒**（绝不因为别人的插件
  出问题而让轮盘报错）；
* 只读不写，不去改别人的状态。

本模块不 import astrbot：``stars`` 是一个返回"插件元数据或实例列表"的可调用对象，
由 ``main.py`` 注入（那边拿到的是 ``context.get_all_stars()``）。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .players import PlayerIdentity

#: 认作"喝酒插件"的目录名 / 插件名（也接受模块路径里含这些片段）
DRUNK_PLUGIN_HINTS: tuple[str, ...] = (
    "astrbot_plugin_repeater",
    "repeater",
)

#: 可能的状态读取方法，按顺序试：(属性路径..., 方法名)
DRUNK_PATHS: tuple[tuple[str, ...], ...] = (
    ("engine", "drunk_remaining"),
    ("engine", "drunkenness"),
    ("drunk_remaining",),
    ("drunkenness",),
)


@dataclass(frozen=True, slots=True)
class DrunkState:
    """一次醉酒查询的结果。"""

    source: str = ""
    """读到状态的插件名；空字符串表示没找到（此时按清醒处理）。"""

    seconds: int = 0
    """还剩多少秒醒酒；0 = 清醒。"""

    @property
    def known(self) -> bool:
        return bool(self.source)

    @property
    def drunk(self) -> bool:
        return self.seconds > 0


def group_key_for(identity: PlayerIdentity) -> str:
    """与对方插件一致的群标识：``平台:群``。"""
    return f"{identity.platform}:{identity.group_id}"


def _seconds(value: Any) -> int:
    """把对方的返回值收敛成"还剩几秒"。"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(float(value)), 0)
        except ValueError:
            return 0
    return 0


class DrunkSource:
    """从别的插件读醉酒状态；读不到就当清醒。"""

    def __init__(
        self,
        stars: Callable[[], Sequence[Any]] | None = None,
        *,
        hints: Sequence[str] = DRUNK_PLUGIN_HINTS,
        paths: Sequence[tuple[str, ...]] = DRUNK_PATHS,
    ) -> None:
        self._stars = stars
        self._hints = tuple(hint.lower() for hint in hints)
        self._paths = tuple(paths)
        #: 上一次成功读到状态的插件名（给启动日志 / 诊断用）
        self.source_name: str = ""

    # ---------------------------------------------------------------- 找插件

    def _matches(self, item: Any) -> bool:
        """这个插件像不像"喝酒插件"。"""
        for attr in ("root_dir_name", "name", "module_path", "display_name"):
            text = str(getattr(item, attr, "") or "").lower()
            if text and any(hint in text for hint in self._hints):
                return True
        module = type(item).__module__.lower()
        return any(hint in module for hint in self._hints)

    def instances(self) -> list[tuple[str, Any]]:
        """插件表里可能是喝酒插件的 ``(名字, 实例)``。"""
        if self._stars is None:
            return []
        try:
            items = list(self._stars() or ())
        except Exception:  # pragma: no cover - 宿主接口异常时静默降级
            return []
        found: list[tuple[str, Any]] = []
        for item in items:
            if not self._matches(item):
                continue
            if getattr(item, "activated", True) is False:
                continue
            # StarMetadata → 取里面那个活着的实例；已经是实例就自己上
            instance = getattr(item, "star_cls", None) or item
            name = str(
                getattr(item, "root_dir_name", "")
                or getattr(item, "name", "")
                or type(instance).__name__,
            )
            found.append((name, instance))
        return found

    def _probe(self, instance: Any) -> Callable[..., Any] | None:
        """从实例上摸出可调用的状态读取函数。"""
        for path in self._paths:
            target = instance
            for attr in path[:-1]:
                target = getattr(target, attr, None)
                if target is None:
                    break
            if target is None:
                continue
            method = getattr(target, path[-1], None)
            if callable(method):
                return method
        return None

    # ---------------------------------------------------------------- 读状态

    async def state(self, identity: PlayerIdentity) -> DrunkState:
        """这个群现在醉着吗（读不到就是清醒）。"""
        key = group_key_for(identity)
        for name, instance in self.instances():
            probe = self._probe(instance)
            if probe is None:
                continue
            value = await self._call(probe, key)
            if value is _UNREADABLE:
                continue
            self.source_name = name
            return DrunkState(source=name, seconds=_seconds(value))
        return DrunkState()

    async def _call(self, probe: Callable[..., Any], key: str) -> Any:
        """调对方的方法；先带群 key，签名不收 key 再空参试一次。"""
        for args in ((key,), ()):
            try:
                value = probe(*args)
            except TypeError:
                continue  # 签名对不上，换一种叫法
            except Exception:
                return _UNREADABLE
            if inspect.isawaitable(value):
                try:
                    value = await value
                except Exception:
                    return _UNREADABLE
            return value
        return _UNREADABLE

    async def remaining(self, identity: PlayerIdentity) -> int:
        return (await self.state(identity)).seconds


class _Unreadable:
    """占位：对方抛异常了，换下一个候选。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<unreadable>"


_UNREADABLE = _Unreadable()
