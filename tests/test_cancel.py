from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from conftest import write_pipeline
from onprem_runner.config import load_config
from onprem_runner.errors import ConfigError
from onprem_runner.models import RunResult, StepResult
from onprem_runner.runner import Runner


def start_in_background(
    runner: Runner, **kwargs: object
) -> tuple[threading.Thread, list[RunResult]]:
    box: list[RunResult] = []
    thread = threading.Thread(target=lambda: box.append(runner.run(**kwargs)))  # type: ignore[arg-type]
    thread.start()
    return thread, box


def wait_until(predicate, timeout: float = 15.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def make_runner(project: Path, body: str, **kwargs: object) -> Runner:
    return Runner(load_config(write_pipeline(project, body)), project, **kwargs)  # type: ignore[arg-type]


# -- cancellation ---------------------------------------------------------------------


def test_cancel_kills_the_running_step_and_skips_the_rest(project: Path) -> None:
    started: list[str] = []

    def on_event(event: str, step_id: str, result: StepResult | None) -> None:
        if event == "step_started":
            started.append(step_id)

    runner = make_runner(
        project,
        "name: p\nsteps:\n"
        "  - {id: slow, type: python, script: scripts/slow.py}\n"
        "  - {id: after, type: python, script: scripts/ok.py, needs: [slow]}\n",
        on_event=on_event,
    )
    thread, box = start_in_background(runner)
    wait_until(lambda: "slow" in started)
    time.sleep(0.3)  # let the process actually start
    began = time.monotonic()
    runner.cancel()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert time.monotonic() - began < 8  # slow.py sleeps 10s; it was killed, not awaited

    result = box[0]
    assert result.status == "cancelled"
    assert {s.id: s.status for s in result.steps} == {"slow": "cancelled", "after": "skipped"}
    assert result.steps[0].exit_code is None


def test_cancel_stops_retries(project: Path) -> None:
    runner = make_runner(
        project,
        "name: p\nsteps:\n"
        "  - {id: flaky, type: python, script: scripts/fail.py, retries: 5, retry_delay: 0.3}\n",
    )
    thread, box = start_in_background(runner)
    time.sleep(0.8)
    runner.cancel()
    thread.join(timeout=15)
    assert box[0].steps[0].attempts < 6  # did not use every retry
    assert box[0].status == "cancelled"


def test_cancel_kills_independent_running_steps_in_parallel(project: Path) -> None:
    runner = make_runner(
        project,
        "name: p\nsteps:\n"
        "  - {id: a, type: python, script: scripts/slow.py}\n"
        "  - {id: b, type: python, script: scripts/slow.py}\n",
    )
    thread, box = start_in_background(runner)
    time.sleep(1.0)
    runner.cancel()
    thread.join(timeout=15)
    assert {s.id: s.status for s in box[0].steps} == {"a": "cancelled", "b": "cancelled"}


def test_a_new_run_after_a_cancelled_one_works(project: Path) -> None:
    runner = make_runner(
        project, "name: p\nsteps:\n  - {id: s, type: python, script: scripts/ok.py}\n"
    )
    runner.cancel()  # a stale cancel must not poison the next run
    assert runner.run().status == "success"


def test_cancelled_runs_do_not_trigger_failure_notifications(project: Path) -> None:
    body = (
        "name: p\nsteps:\n  - {id: slow, type: python, script: scripts/slow.py}\n"
        "notifiers:\n"
        "  - {type: file, path: 'out/failure.txt', format: txt, when: failure}\n"
        "  - {type: file, path: 'out/success.txt', format: txt, when: success}\n"
        "  - {type: file, path: 'out/always.txt', format: txt, when: always}\n"
    )
    runner = make_runner(project, body)
    thread, _ = start_in_background(runner)
    time.sleep(1.0)
    runner.cancel()
    thread.join(timeout=15)
    assert (project / "out" / "always.txt").exists()
    assert not (project / "out" / "failure.txt").exists()
    assert not (project / "out" / "success.txt").exists()


# -- partial runs ----------------------------------------------------------------------


GRAPH = """
name: p
steps:
  - {id: a, type: python, script: scripts/ok.py}
  - {id: b, type: python, script: scripts/ok.py, needs: [a]}
  - {id: c, type: python, script: scripts/ok.py, needs: [b]}
  - {id: alert, type: python, script: scripts/ok.py, needs: [{step: c, when: failure}]}
"""


def test_only_runs_the_selected_steps(project: Path) -> None:
    result = make_runner(project, GRAPH).run(only=["b"])
    assert [(s.id, s.status) for s in result.steps] == [("b", "success")]
    assert result.status == "success"


def test_only_ignores_edges_to_steps_outside_the_selection(project: Path) -> None:
    result = make_runner(project, GRAPH).run(only=["b", "c"])
    assert {s.id: s.status for s in result.steps} == {"b": "success", "c": "success"}


def test_only_keeps_dependencies_among_selected_steps(project: Path) -> None:
    result = make_runner(project, GRAPH).run(only=["a", "b", "c", "alert"])
    assert {s.id: s.status for s in result.steps} == {
        "a": "success",
        "b": "success",
        "c": "success",
        "alert": "skipped",  # c succeeded, so the failure branch is skipped
    }


def test_only_with_a_failing_step_still_fails_the_run(project: Path) -> None:
    body = (
        "name: p\nsteps:\n"
        "  - {id: bad, type: python, script: scripts/fail.py}\n"
        "  - {id: ok, type: python, script: scripts/ok.py}\n"
    )
    result = make_runner(project, body).run(only=["bad"])
    assert result.status == "failed"
    assert [s.id for s in result.steps] == ["bad"]


def test_only_rejects_unknown_steps(project: Path) -> None:
    with pytest.raises(ConfigError, match="nope"):
        make_runner(project, GRAPH).run(only=["a", "nope"])
