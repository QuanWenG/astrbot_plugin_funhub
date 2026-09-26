"""龟龟乐园的业务实现。

本包刻意不 import ``astrbot``：所有模块都能脱离 AstrBot 运行时独立导入与测试，
只有仓库根目录的 ``main.py`` 负责把它接到 AstrBot 的事件与 Web API 上。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
