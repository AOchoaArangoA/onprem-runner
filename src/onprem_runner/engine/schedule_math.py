"""When does a ``schedule:`` block fire? Pure functions over naive local datetimes.

Alignment is deterministic, so a restart never shifts the rhythm:

* ``minutes`` / ``hourly``: multiples of the interval counted from local midnight
  (``every: 15`` minutes fires at :00 :15 :30 :45; the count restarts each midnight).
* ``daily``: days whose ordinal is a multiple of ``every``.
* ``weekly``: Monday-based weeks whose index is a multiple of ``every``, on the chosen weekdays.
* ``startup``: has no clock time; it fires once each time the engine starts.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from onprem_runner.models import ScheduleConfig

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
MAX_CATCH_UP_STEPS = 100_000


def _at(schedule: ScheduleConfig) -> time:
    hour, minute = (schedule.at or "00:00").split(":")
    return time(int(hour), int(minute))


def next_occurrence(schedule: ScheduleConfig, after: datetime) -> datetime | None:
    """The first firing time strictly after ``after`` (``None`` for ``startup``)."""
    every = schedule.every
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)

    if schedule.frequency == "startup":
        return None

    if schedule.frequency in ("minutes", "hourly"):
        step = timedelta(minutes=every if schedule.frequency == "minutes" else every * 60)
        index = (after - day) // step + 1
        candidate = day + index * step
        return candidate if candidate.date() == after.date() else day + timedelta(days=1)

    at = _at(schedule)
    wanted = {_WEEKDAYS[d] for d in schedule.days}
    for offset in range(0, 7 * every + 8):
        date = (day + timedelta(days=offset)).date()
        if schedule.frequency == "daily":
            qualifies = date.toordinal() % every == 0
        else:  # weekly
            qualifies = date.weekday() in wanted and ((date.toordinal() - 1) // 7) % every == 0
        if qualifies:
            candidate = datetime.combine(date, at)
            if candidate > after:
                return candidate
    return None


def latest_occurrence(schedule: ScheduleConfig, after: datetime, upto: datetime) -> datetime | None:
    """The most recent firing time in ``(after, upto]``, or ``None`` if there is none."""
    latest: datetime | None = None
    cursor = after
    for _ in range(MAX_CATCH_UP_STEPS):
        candidate = next_occurrence(schedule, cursor)
        if candidate is None or candidate > upto:
            break
        latest = cursor = candidate
    return latest
