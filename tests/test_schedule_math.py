from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from onprem_runner.engine.schedule_math import latest_occurrence, next_occurrence
from onprem_runner.models import ScheduleConfig


def sched(**kwargs: Any) -> ScheduleConfig:
    return ScheduleConfig(**kwargs)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- minutes and hours -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-05 10:07:00", "2026-10-05 10:15:00"),
        ("2026-10-05 10:15:00", "2026-10-05 10:30:00"),  # strictly after
        ("2026-10-05 10:44:59", "2026-10-05 10:45:00"),
        ("2026-10-05 23:50:00", "2026-10-06 00:00:00"),  # rolls over midnight
    ],
)
def test_every_15_minutes_is_aligned_to_the_clock(now: str, expected: str) -> None:
    assert next_occurrence(sched(frequency="minutes", every=15), at(now)) == at(expected)


def test_minute_schedules_that_do_not_divide_the_day_restart_at_midnight() -> None:
    s = sched(frequency="minutes", every=7)
    # 1440 = 205*7 + 5: the last slot of the day is 23:55 (205th) and the next one is midnight.
    assert next_occurrence(s, at("2026-10-05 23:55:00")) == at("2026-10-06 00:00:00")
    assert next_occurrence(s, at("2026-10-06 00:00:00")) == at("2026-10-06 00:07:00")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-05 10:00:00", "2026-10-05 12:00:00"),
        ("2026-10-05 12:00:01", "2026-10-05 14:00:00"),
        ("2026-10-05 22:30:00", "2026-10-06 00:00:00"),
    ],
)
def test_every_2_hours(now: str, expected: str) -> None:
    assert next_occurrence(sched(frequency="hourly", every=2), at(now)) == at(expected)


def test_hourly_every_hour() -> None:
    assert next_occurrence(sched(frequency="hourly"), at("2026-10-05 10:20:00")) == at(
        "2026-10-05 11:00:00"
    )


# -- daily -------------------------------------------------------------------------------


def test_daily_picks_today_if_the_time_has_not_passed_else_tomorrow() -> None:
    s = sched(frequency="daily", at="08:30")
    assert next_occurrence(s, at("2026-10-05 07:00:00")) == at("2026-10-05 08:30:00")
    assert next_occurrence(s, at("2026-10-05 08:30:00")) == at("2026-10-06 08:30:00")
    assert next_occurrence(s, at("2026-10-05 20:00:00")) == at("2026-10-06 08:30:00")


def test_every_third_day_keeps_a_stable_rhythm() -> None:
    s = sched(frequency="daily", at="06:00", every=3)
    first = next_occurrence(s, at("2026-10-05 00:00:00"))
    assert first is not None
    second = next_occurrence(s, first)
    third = next_occurrence(s, second) if second else None
    assert second is not None and third is not None
    assert (second - first).days == 3
    assert (third - second).days == 3
    # Asking from another starting point lands on the same grid of days.
    again = next_occurrence(s, at("2026-10-05 12:00:00"))
    assert again is not None and (again.date().toordinal() - first.date().toordinal()) % 3 == 0


# -- weekly -------------------------------------------------------------------------------


def test_weekly_on_selected_days() -> None:
    s = sched(frequency="weekly", at="02:00", days=["MON", "WED", "FRI"])
    assert next_occurrence(s, at("2026-10-05 12:00:00")) == at("2026-10-07 02:00:00")  # Mon -> Wed
    assert next_occurrence(s, at("2026-10-09 03:00:00")) == at("2026-10-12 02:00:00")  # Fri -> Mon
    assert next_occurrence(s, at("2026-10-05 01:00:00")) == at("2026-10-05 02:00:00")  # same Monday


def test_every_second_week() -> None:
    s = sched(frequency="weekly", at="09:00", days=["TUE"], every=2)
    first = next_occurrence(s, at("2026-10-01 00:00:00"))
    assert first is not None and first.weekday() == 1
    second = next_occurrence(s, first)
    assert second is not None and (second - first).days == 14


def test_sunday_belongs_to_the_week_that_started_on_monday() -> None:
    s = sched(frequency="weekly", at="09:00", days=["SUN"])
    assert next_occurrence(s, at("2026-10-05 00:00:00")) == at("2026-10-11 09:00:00")


# -- startup ---------------------------------------------------------------------------------


def test_startup_has_no_clock_time() -> None:
    assert next_occurrence(sched(frequency="startup"), at("2026-10-05 00:00:00")) is None


# -- invariants ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schedule",
    [
        sched(frequency="minutes", every=1),
        sched(frequency="minutes", every=13),
        sched(frequency="hourly", every=5),
        sched(frequency="daily", at="23:59", every=2),
        sched(frequency="weekly", at="00:00", days=["MON", "SUN"], every=3),
    ],
)
def test_occurrences_are_strictly_increasing_and_never_in_the_past(
    schedule: ScheduleConfig,
) -> None:
    cursor = at("2026-12-28 21:37:41")  # also crosses a year boundary
    for _ in range(200):
        following = next_occurrence(schedule, cursor)
        assert following is not None
        assert following > cursor
        cursor = following


# -- catching up ----------------------------------------------------------------------------------


def test_latest_occurrence_returns_only_the_most_recent_missed_slot() -> None:
    s = sched(frequency="hourly")
    assert latest_occurrence(s, at("2026-10-05 08:30:00"), at("2026-10-05 11:20:00")) == at(
        "2026-10-05 11:00:00"
    )


def test_latest_occurrence_is_none_when_nothing_was_missed() -> None:
    s = sched(frequency="daily", at="08:00")
    assert latest_occurrence(s, at("2026-10-05 08:00:00"), at("2026-10-05 23:00:00")) is None


def test_latest_occurrence_handles_long_gaps() -> None:
    s = sched(frequency="minutes", every=5)
    result = latest_occurrence(s, at("2026-10-01 00:00:00"), at("2026-10-20 12:03:00"))
    assert result == at("2026-10-20 12:00:00")
