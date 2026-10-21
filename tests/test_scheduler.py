from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from onprem_runner.engine import BadRequest, Engine, NotFound
from onprem_runner.engine import scheduler as scheduler_module


class ReverseCipher:
    def encrypt(self, data: bytes) -> bytes:
        return b"enc:" + data[::-1]

    def decrypt(self, data: bytes) -> bytes:
        return data[4:][::-1]


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Clock:
    def __init__(self, now: str) -> None:
        self.now = at(now)

    def __call__(self) -> datetime:
        return self.now


FLOW = (
    "name: Scheduled flow\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"
    "schedule: {%s}\n"
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "ok.py").write_text("print('ran')\n", encoding="utf-8")
    (ws / "scripts" / "slow.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    return ws


def make_engine(tmp_path: Path, workspace: Path, clock: Clock) -> Engine:
    engine = Engine(tmp_path / "data", workspace=workspace, cipher=ReverseCipher())
    engine.start(scheduler=False)
    engine.scheduler.clock = clock
    return engine


@pytest.fixture
def clock() -> Clock:
    return Clock("2026-10-05 10:07:00")


@pytest.fixture
def engine(tmp_path: Path, workspace: Path, clock: Clock) -> Iterator[Engine]:
    eng = make_engine(tmp_path, workspace, clock)
    yield eng
    eng.close()


def wait_idle(engine: Engine) -> None:
    deadline = time.monotonic() + 30
    while engine.active_runs():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    engine.db.flush()


def runs(engine: Engine) -> list[dict[str, Any]]:
    wait_idle(engine)
    return engine.list_runs()


# -- firing -----------------------------------------------------------------------------


def test_fires_when_the_slot_arrives_and_not_before(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: minutes, every: 15", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T10:15:00"

    clock.now = at("2026-10-05 10:14:59")
    engine.scheduler.tick(clock.now)
    assert runs(engine) == []

    clock.now = at("2026-10-05 10:15:00")
    engine.scheduler.tick(clock.now)
    [run] = runs(engine)
    assert run["trigger"] == "schedule"
    assert run["status"] == "success"
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T10:30:00"

    engine.scheduler.tick(at("2026-10-05 10:15:01"))  # the same slot must not fire twice
    assert len(runs(engine)) == 1


def test_a_new_schedule_does_not_fire_retroactively(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    clock.now = at("2026-10-05 09:00:00")  # the user adds a 07:30 schedule at 09:00
    (workspace / "flow.yaml").write_text(FLOW % 'frequency: daily, at: "07:30"', encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    assert runs(engine) == []
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-06T07:30:00"


def test_events_announce_what_the_scheduler_did(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: hourly", encoding="utf-8")
    sub = engine.bus.subscribe()
    engine.scheduler.start(run_thread=False)
    engine.scheduler.tick(at("2026-10-05 11:00:00"))
    seen = []
    while (event := sub.get(timeout=0.5)) is not None:
        seen.append(event)
    fired = [e for e in seen if e["type"] == "schedule_fired"]
    assert len(fired) == 1
    assert fired[0]["file"] == "flow.yaml" and fired[0]["slot"] == "2026-10-05T11:00:00"
    wait_idle(engine)


# -- missed slots -----------------------------------------------------------------------


def seed_state(engine: Engine, last_fire: str, paused: bool = False) -> None:
    engine.scheduler.state_path.write_text(
        json.dumps({"jobs": {"flow.yaml": {"last_fire": last_fire, "paused": paused}}}),
        encoding="utf-8",
    )


def test_a_missed_slot_runs_once_when_the_app_opens_again(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % 'frequency: daily, at: "07:30"', encoding="utf-8")
    seed_state(engine, "2026-10-02T07:30:00")  # closed for three days
    clock.now = at("2026-10-06 12:00:00")
    engine.scheduler.start(run_thread=False)
    [run] = runs(engine)  # one run, not four
    assert run["trigger"] == "schedule"
    snapshot = engine.scheduler.snapshot()["flow.yaml"]
    assert snapshot["last_fire"] == "2026-10-06T07:30:00"
    assert snapshot["next_run"] == "2026-10-07T07:30:00"


def test_catch_up_can_be_disabled(engine: Engine, workspace: Path, clock: Clock) -> None:
    (workspace / "flow.yaml").write_text(
        FLOW % 'frequency: daily, at: "07:30", catch_up: false', encoding="utf-8"
    )
    seed_state(engine, "2026-10-05T07:30:00")
    clock.now = at("2026-10-06 12:00:00")
    sub = engine.bus.subscribe()
    engine.scheduler.start(run_thread=False)
    engine.scheduler.tick(clock.now)  # the stale slot is discarded, not run
    assert runs(engine) == []
    reasons = []
    while (event := sub.get(timeout=0.3)) is not None:
        if event["type"] == "schedule_skipped":
            reasons.append(event["reason"])
    assert reasons == ["missed"]
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-07T07:30:00"


def test_after_a_long_sleep_only_the_latest_slot_runs(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: minutes, every: 5", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    clock.now = at("2026-10-05 13:03:00")  # the laptop slept for three hours
    engine.scheduler.tick(clock.now)
    assert len(runs(engine)) == 1
    assert engine.scheduler.snapshot()["flow.yaml"]["last_fire"] == "2026-10-05T13:00:00"


def test_a_restart_does_not_repeat_a_slot_that_already_ran(
    tmp_path: Path, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: hourly", encoding="utf-8")
    first = make_engine(tmp_path, workspace, clock)
    try:
        first.scheduler.start(run_thread=False)
        clock.now = at("2026-10-05 11:00:00")
        first.scheduler.tick(clock.now)
        wait_idle(first)
    finally:
        first.close()

    clock.now = at("2026-10-05 11:20:00")
    second = make_engine(tmp_path, workspace, clock)
    try:
        second.scheduler.start(run_thread=False)
        assert len(runs(second)) == 1  # only the 11:00 run from before, nothing replayed
    finally:
        second.close()


# -- conditions that must not fire ------------------------------------------------------


def test_a_pipeline_that_is_still_running_is_skipped_not_queued(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(
        "name: Slow\nsteps:\n  - {id: a, type: python, script: scripts/slow.py}\n"
        "schedule: {frequency: minutes, every: 1}\n",
        encoding="utf-8",
    )
    engine.scheduler.start(run_thread=False)
    sub = engine.bus.subscribe()
    clock.now = at("2026-10-05 10:08:00")
    engine.scheduler.tick(clock.now)  # starts the slow run
    time.sleep(0.5)
    clock.now = at("2026-10-05 10:09:00")
    engine.scheduler.tick(clock.now)  # still running: skipped
    skipped = []
    while (event := sub.get(timeout=0.3)) is not None:
        if event["type"] == "schedule_skipped":
            skipped.append(event["reason"])
    assert len(skipped) == 1 and "already running" in skipped[0]
    assert len(engine.active_runs()) == 1
    engine.cancel_run(engine.active_runs()[0]["run_id"])
    wait_idle(engine)
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T10:10:00"


def test_pausing_stops_firing_and_resuming_does_not_replay(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: hourly", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    engine.set_schedule_paused("flow.yaml", True)
    assert engine.scheduler.snapshot()["flow.yaml"]["paused"] is True
    assert engine.list_pipelines()[0]["paused"] is True

    clock.now = at("2026-10-05 11:00:00")
    engine.scheduler.tick(clock.now)
    assert runs(engine) == []

    clock.now = at("2026-10-05 12:30:00")
    engine.set_schedule_paused("flow.yaml", False)
    engine.scheduler.tick(clock.now)
    assert runs(engine) == []  # the missed 11:00 and 12:00 slots are not replayed
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T13:00:00"


def test_pause_survives_a_restart(tmp_path: Path, workspace: Path, clock: Clock) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: hourly", encoding="utf-8")
    first = make_engine(tmp_path, workspace, clock)
    try:
        first.scheduler.start(run_thread=False)
        first.set_schedule_paused("flow.yaml", True)
    finally:
        first.close()
    second = make_engine(tmp_path, workspace, clock)
    try:
        second.scheduler.start(run_thread=False)
        assert second.scheduler.snapshot()["flow.yaml"]["paused"] is True
    finally:
        second.close()


def test_startup_schedules_run_once_when_the_engine_starts(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: startup", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    assert len(runs(engine)) == 1
    clock.now += timedelta(hours=5)
    engine.scheduler.tick(clock.now)
    assert len(runs(engine)) == 1  # never again until the next start
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] is None


# -- changes to the schedules -----------------------------------------------------------


def test_editing_the_schedule_takes_effect_from_now(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    path = workspace / "flow.yaml"
    path.write_text(FLOW % "frequency: hourly", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T11:00:00"
    clock.now = at("2026-10-05 10:40:00")
    path.write_text(FLOW % 'frequency: daily, at: "18:00"', encoding="utf-8")
    engine.scheduler.refresh(clock.now)
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T18:00:00"


def test_removing_the_schedule_or_breaking_the_file_unschedules_it(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    path = workspace / "flow.yaml"
    path.write_text(FLOW % "frequency: hourly", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    assert "flow.yaml" in engine.scheduler.snapshot()
    path.write_text("name: Broken\nsteps: [", encoding="utf-8")
    engine.scheduler.refresh(clock.now)
    assert "flow.yaml" not in engine.scheduler.snapshot()
    path.write_text("name: P\nsteps:\n  - {id: a, type: shell, command: x}\n", encoding="utf-8")
    engine.scheduler.refresh(clock.now)
    assert engine.scheduler.snapshot() == {}


def test_saving_in_the_editor_refreshes_the_scheduler(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(
        "name: P\nsteps:\n  - {id: a, type: shell, command: echo x}\n", encoding="utf-8"
    )
    engine.scheduler.start(run_thread=False)
    assert engine.scheduler.snapshot() == {}
    graph = engine.get_graph("flow.yaml")
    graph["schedule"] = {
        "frequency": "hourly",
        "at": None,
        "every": 1,
        "days": [],
        "catch_up": True,
    }
    engine.save_graph("flow.yaml", graph, etag=graph["etag"])
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T11:00:00"


def test_pause_needs_a_real_pipeline(engine: Engine) -> None:
    with pytest.raises(NotFound):
        engine.set_schedule_paused("ghost.yaml", True)


def test_damaged_state_file_does_not_stop_the_scheduler(
    engine: Engine, workspace: Path, clock: Clock
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: hourly", encoding="utf-8")
    engine.scheduler.state_path.write_text("{not json", encoding="utf-8")
    engine.scheduler.start(run_thread=False)
    assert "flow.yaml" in engine.scheduler.snapshot()


# -- the real thread --------------------------------------------------------------------


def test_the_background_thread_fires_by_itself(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "TICK_SECONDS", 0.05)
    clock = Clock("2026-10-05 10:14:50")
    (workspace / "flow.yaml").write_text(FLOW % "frequency: minutes, every: 15", encoding="utf-8")
    engine = Engine(tmp_path / "data", workspace=workspace, cipher=ReverseCipher())
    engine.scheduler.clock = clock
    engine.start()  # starts the scheduler thread
    try:
        time.sleep(0.4)
        assert engine.list_runs() == []
        clock.now = at("2026-10-05 10:15:05")  # time passes
        deadline = time.monotonic() + 15
        while not engine.list_runs():
            assert time.monotonic() < deadline, "the scheduler thread never fired"
            time.sleep(0.05)
        wait_idle(engine)
        assert engine.list_runs()[0]["trigger"] == "schedule"
    finally:
        engine.close()


def test_a_failing_start_does_not_kill_the_scheduler(
    engine: Engine, workspace: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "flow.yaml").write_text(FLOW % "frequency: hourly", encoding="utf-8")
    engine.scheduler.start(run_thread=False)

    def boom(*args: Any, **kwargs: Any) -> str:
        raise BadRequest("simulated failure")

    monkeypatch.setattr(engine, "start_run", boom)
    clock.now = at("2026-10-05 11:00:00")
    engine.scheduler.tick(clock.now)  # must not raise
    assert engine.scheduler.snapshot()["flow.yaml"]["next_run"] == "2026-10-05T12:00:00"
