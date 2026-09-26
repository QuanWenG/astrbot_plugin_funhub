"""结算日换算：重置时刻、跨月跨年、时区回退。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from funhub.clock import previous_day, resolve_zone, settlement_date, settlement_day_key

HK = timezone(timedelta(hours=8))


def test_before_reset_counts_as_previous_day():
    moment = datetime(2026, 1, 2, 3, 59, tzinfo=HK)
    assert settlement_date(moment, zone=HK, reset_hour=4) == date(2026, 1, 1)


def test_at_reset_starts_a_new_day():
    moment = datetime(2026, 1, 2, 4, 0, tzinfo=HK)
    assert settlement_date(moment, zone=HK, reset_hour=4) == date(2026, 1, 2)


def test_late_evening_stays_on_the_same_day():
    moment = datetime(2026, 1, 2, 23, 59, tzinfo=HK)
    assert settlement_date(moment, zone=HK, reset_hour=4) == date(2026, 1, 2)


def test_zero_reset_hour_is_the_calendar_day():
    moment = datetime(2026, 1, 2, 0, 30, tzinfo=HK)
    assert settlement_date(moment, zone=HK, reset_hour=0) == date(2026, 1, 2)


def test_custom_reset_hour():
    moment = datetime(2026, 1, 2, 19, 0, tzinfo=HK)
    assert settlement_date(moment, zone=HK, reset_hour=20) == date(2026, 1, 1)


def test_naive_datetime_is_read_as_local_wall_clock():
    naive = datetime(2026, 1, 2, 3, 30)
    assert settlement_date(naive, zone=HK, reset_hour=4) == date(2026, 1, 1)


def test_month_and_year_boundaries():
    assert settlement_date(
        datetime(2026, 1, 1, 2, 0, tzinfo=HK), zone=HK, reset_hour=4
    ) == date(2025, 12, 31)
    assert settlement_date(
        datetime(2026, 3, 1, 1, 0, tzinfo=HK), zone=HK, reset_hour=4
    ) == date(2026, 2, 28)


def test_timezone_conversion_happens_before_shifting():
    moment = datetime(2026, 1, 2, 20, 0, tzinfo=timezone.utc)  # HK 时间 1/3 04:00
    assert settlement_date(moment, zone=HK, reset_hour=4) == date(2026, 1, 3)


def test_day_key_is_iso_text():
    key = settlement_day_key(datetime(2026, 1, 2, 3, 0, tzinfo=HK), zone=HK, reset_hour=4)
    assert key == "2026-01-01"


def test_previous_day_helper():
    assert previous_day(date(2026, 1, 1)) == date(2025, 12, 31)


@pytest.mark.parametrize("reset_hour", [-1, 24, 100])
def test_out_of_range_reset_hour_raises(reset_hour: int):
    with pytest.raises(ValueError):
        settlement_date(datetime(2026, 1, 1, tzinfo=HK), zone=HK, reset_hour=reset_hour)


def test_non_integer_reset_hour_raises():
    with pytest.raises(TypeError):
        settlement_date(datetime(2026, 1, 1, tzinfo=HK), zone=HK, reset_hour=True)


def test_resolve_zone_returns_a_zone():
    zone, warning = resolve_zone("Asia/Hong_Kong")
    assert zone is not None
    assert warning is None or "回退" in warning


def test_resolve_zone_falls_back_instead_of_raising():
    zone, warning = resolve_zone("Not/A-Real-Zone")
    assert zone is not None
    assert warning and "回退" in warning
    # 回退后的时区仍然能算出结算日
    assert settlement_date(datetime(2026, 1, 2, 3, 0, tzinfo=zone), zone=zone, reset_hour=4) == date(
        2026,
        1,
        1,
    )
