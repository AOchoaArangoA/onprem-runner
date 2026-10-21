"""The engine's own scheduler: runs pipelines at their ``schedule:`` times while the app is open.

State (last fired slot, paused flag) is kept in ``scheduler.json`` so a restart neither repeats
nor silently loses a slot. When the app was closed at a scheduled time it runs the most recent
missed slot once on startup (``catch_up: false`` in the schedule disables that).
Windows Task Scheduler (``opr schedule``) remains available for runs that must happen without
the app open.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from onprem_runner.engine.schedule_math import latest_occurrence, next_occurrence
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.models import ScheduleConfig

if TYPE_CHECKING:
    from onprem_runner.engine.core import Engine

log = logging.getLogger(LOGGER_NAME)

TICK_SECONDS = 1.0
REFRESH_SECONDS = 30.0
LATE_GRACE = timedelta(minutes=2)  # later than this a slot counts as missed (laptop was asleep)


@dataclass
class JobState:
    last_fire: datetime | None
    paused: bool = False


@dataclass
class Job:
    name: str
    schedule: ScheduleConfig
    next_fire: datetime | None


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        state_path: Path,
        *,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.engine = engine
        self.state_path = state_path
        self.clock = clock
        self._lock = threading.RLock()
        self._state: dict[str, JobState] = {}
        self._jobs: dict[str, Job] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_refresh = datetime.min

    # -- lifecycle ---------------------------------------------------------------------
    def start(self, *, run_thread: bool = True) -> None:
        now = self.clock()
        self._load_state()
        self.refresh(now)
        self._catch_up_and_startup(now)
        if run_thread:
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick(self.clock())
            except Exception:
                log.exception("scheduler tick failed; continuing")

    # -- reading the schedules ---------------------------------------------------------
    def refresh(self, now: datetime | None = None) -> None:
        """Re-read every pipeline's schedule block (after edits, or periodically)."""
        now = now or self.clock()
        found = self.engine.scheduled_pipelines()
        with self._lock:
            seen: set[str] = set()
            for rel, name, schedule in found:
                seen.add(rel)
                state = self._state.get(rel)
                if state is None:
                    # First sight: only slots from now on count, no retroactive catch-up.
                    state = self._state[rel] = JobState(last_fire=now)
                job = self._jobs.get(rel)
                if job is not None and job.schedule == schedule:
                    job.name = name
                    continue
                if job is not None:  # the schedule was edited: it applies from now on
                    state.last_fire = now
                after = state.last_fire or now
                self._jobs[rel] = Job(name, schedule, next_occurrence(schedule, after))
            for rel in set(self._jobs) - seen:
                del self._jobs[rel]
            self._last_refresh = now
            self._save_state()

    # -- firing ------------------------------------------------------------------------
    def _catch_up_and_startup(self, now: datetime) -> None:
        with self._lock:
            jobs = list(self._jobs.items())
        for rel, job in jobs:
            state = self._state[rel]
            if state.paused:
                continue
            if job.schedule.frequency == "startup":
                self._fire(rel, job, slot=now, kind="startup")
                continue
            if not job.schedule.catch_up or state.last_fire is None:
                continue
            missed = latest_occurrence(job.schedule, state.last_fire, now)
            if missed is not None:
                self._fire(rel, job, slot=missed, kind="catch_up")

    def tick(self, now: datetime) -> None:
        """Fire whatever is due at ``now`` (called every second by the scheduler thread)."""
        if (now - self._last_refresh).total_seconds() >= REFRESH_SECONDS:
            self.refresh(now)
        with self._lock:
            due = [
                (rel, job)
                for rel, job in self._jobs.items()
                if job.next_fire is not None
                and job.next_fire <= now
                and not self._state[rel].paused
            ]
        for rel, job in due:
            assert job.next_fire is not None
            # If several slots passed (the machine slept), only the latest one is considered.
            state = self._state[rel]
            latest = latest_occurrence(job.schedule, state.last_fire or job.next_fire, now)
            slot = latest or job.next_fire
            late = now - slot
            if late > LATE_GRACE and not job.schedule.catch_up:
                self._advance(rel, job, slot)
                self.engine.bus.publish(
                    {"type": "schedule_skipped", "file": rel, "reason": "missed"}
                )
                continue
            self._fire(rel, job, slot=slot, kind="schedule")

    def _fire(self, rel: str, job: Job, *, slot: datetime, kind: str) -> None:
        run_id: str | None = None
        reason: str | None = None
        try:
            run_id = self.engine.start_run(rel, trigger="schedule")
        except Exception as exc:  # busy, invalid, missing secret... never stops the scheduler
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("scheduled run of %s not started: %s", rel, reason)
        self._advance(rel, job, slot)
        if run_id is not None:
            self.engine.bus.publish(
                {
                    "type": "schedule_fired",
                    "file": rel,
                    "run_id": run_id,
                    "slot": slot.isoformat(),
                    "kind": kind,
                }
            )
        else:
            self.engine.bus.publish({"type": "schedule_skipped", "file": rel, "reason": reason})

    def _advance(self, rel: str, job: Job, slot: datetime) -> None:
        with self._lock:
            self._state[rel].last_fire = slot
            job.next_fire = next_occurrence(job.schedule, slot)
            self._save_state()
        self.engine.bus.publish({"type": "pipelines_changed"})

    # -- controls and views ------------------------------------------------------------
    def set_paused(self, rel: str, paused: bool) -> None:
        with self._lock:
            state = self._state.setdefault(rel, JobState(last_fire=self.clock()))
            if state.paused and not paused:
                # Resuming never replays what was skipped while paused.
                state.last_fire = self.clock()
                job = self._jobs.get(rel)
                if job is not None:
                    job.next_fire = next_occurrence(job.schedule, state.last_fire)
            state.paused = paused
            self._save_state()
        self.engine.bus.publish({"type": "pipelines_changed"})

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            result: dict[str, dict[str, Any]] = {}
            for rel, job in self._jobs.items():
                state = self._state[rel]
                result[rel] = {
                    "next_run": job.next_fire.isoformat() if job.next_fire else None,
                    "paused": state.paused,
                    "last_fire": state.last_fire.isoformat() if state.last_fire else None,
                    "startup": job.schedule.frequency == "startup",
                }
            return result

    # -- persistence -------------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            jobs = raw["jobs"]
        except (OSError, ValueError, KeyError):
            return
        with self._lock:
            for rel, entry in jobs.items():
                try:
                    last = (
                        datetime.fromisoformat(entry["last_fire"])
                        if entry.get("last_fire")
                        else None
                    )
                    self._state[rel] = JobState(last_fire=last, paused=bool(entry.get("paused")))
                except (ValueError, TypeError, AttributeError):
                    continue

    def _save_state(self) -> None:
        payload = {
            "jobs": {
                rel: {
                    "last_fire": s.last_fire.isoformat() if s.last_fire else None,
                    "paused": s.paused,
                }
                for rel, s in self._state.items()
            }
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.state_path)
        except OSError:
            log.exception("could not save scheduler state")
