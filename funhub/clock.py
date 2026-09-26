"""结算日换算。

插件的一天不是自然日：默认凌晨 4 点重置，凌晨 3:59 签到算前一天。算法与老插件
``services/daily_growth_budget.py`` 保持一致，避免导入的连签在边界上错位。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo


def resolve_zone(name: str) -> tuple[tzinfo, str | None]:
    """返回 ``(时区对象, warning)``。

    缺少 tzdata 的 Windows 环境读不到 ``ZoneInfo``，此时回落到系统本地时区而不是
    抛异常 —— 签到功能必须能用，warning 由调用方写进日志。
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name), None
    except Exception:  # noqa: BLE001 - tzdata 缺失会以多种异常形式出现
        fallback = datetime.now().astimezone().tzinfo or timezone.utc
        return (
            fallback,
            f"无法加载时区 {name!r}，已回退到系统本地时区；"
            "请安装 tzdata 或在配置里改用可用时区",
        )


def local_now(now: datetime | None, zone: tzinfo) -> datetime:
    """把 ``now`` 归一到指定时区；``None`` 表示取当前时间。"""
    if now is None:
        return datetime.now(tz=zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def settlement_date(
    now: datetime | None,
    *,
    zone: tzinfo,
    reset_hour: int,
) -> date:
    """签到结算日：把时间轴向前平移 ``reset_hour`` 小时后的日期。

    ``reset_hour=4`` 时，``2026-01-02 03:30`` 属于 ``2026-01-01``。
    """
    if isinstance(reset_hour, bool) or not isinstance(reset_hour, int):
        raise TypeError("reset_hour 必须是整数")
    if not 0 <= reset_hour <= 23:
        raise ValueError("reset_hour 必须在 0..23 之间")
    local = local_now(now, zone)
    return (local - timedelta(hours=reset_hour)).date()


def settlement_day_key(now: datetime | None, *, zone: tzinfo, reset_hour: int) -> str:
    """结算日的 ``YYYY-MM-DD`` 文本，直接用于入库与比较。"""
    return settlement_date(now, zone=zone, reset_hour=reset_hour).isoformat()


def previous_day(day: date) -> date:
    return day - timedelta(days=1)
