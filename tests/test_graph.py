from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import write_pipeline
from onprem_runner.config import load_config
from onprem_runner.errors import ConfigError
from onprem_runner.models import PipelineConfig, RunResult, StepConfig
from onprem_runner.runner import Runner


@pytest.fixture
def graph(project: Path) -> Path:
    """Adds a script that records when it ran, to measure overlap between steps."""
    (project / "scripts" / "mark.py").write_text(
        "import sys, time, pathlib\n"
        "name, seconds = sys.argv[1], float(sys.argv[2])\n"
        "start = time.time()\ntime.sleep(seconds)\n"
        "pathlib.Path('marks').mkdir(exist_ok=True)\n"
        "pathlib.Path(f'marks/{name}.txt').write_text(f'{start} {time.time()}')\n",
        encoding="utf-8",
    )
    return project


def run(project: Path, body: str) -> RunResult:
    return Runner(load_config(write_pipeline(project, body)), project).run()


def statuses(result: RunResult) -> dict[str, str]:
    return {s.id: s.status for s in result.steps}


def intervals(project: Path, *names: str) -> list[tuple[float, float]]:
    out = []
    for name in names:
        start, end = (project / "marks" / f"{name}.txt").read_text().split()
        out.append((float(start), float(end)))
    return out


def max_concurrency(spans: list[tuple[float, float]]) -> int:
    points = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
    current = peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


# -- models -------------------------------------------------------------------


def test_needs_accepts_ids_and_edges() -> None:
    step = StepConfig.model_validate(
        {"id": "c", "type": "shell", "needs": ["a", {"step": "b", "when": "failure"}]}
    )
    assert step.needs_ids == ["a", "b"]
    assert [e.when for e in step.needs] == ["success", "failure"]


def test_invalid_edge_condition_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StepConfig.model_validate(
            {"id": "c", "type": "shell", "needs": [{"step": "a", "when": "sometimes"}]}
        )


def test_duplicate_edge_is_a_config_error(project: Path) -> None:
    path = write_pipeline(
        project,
        "name: p\nsteps:\n  - {id: a, type: shell, command: x}\n"
        "  - {id: b, type: shell, command: x, needs: [a, {step: a, when: failure}]}\n",
    )
    with pytest.raises(ConfigError, match="more than once"):
        load_config(path)


def test_max_parallel_and_ui_positions_parse() -> None:
    step = {"id": "a", "type": "shell", "command": "x"}
    config = PipelineConfig.model_validate(
        {
            "name": "p",
            "max_parallel": 2,
            "steps": [step],
            "ui": {"positions": {"a": [10, 20.5]}},
        }
    )
    assert config.max_parallel == 2
    assert config.ui is not None
    assert config.ui.positions["a"] == [10, 20.5]
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate({"name": "p", "max_parallel": 0, "steps": [step]})
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(
            {"name": "p", "steps": [step], "ui": {"positions": {"a": [1, 2, 3]}}}
        )


# -- parallel execution ---------------------------------------------------------


def test_independent_steps_run_concurrently(graph: Path) -> None:
    result = run(
        graph,
        """
name: p
steps:
  - {id: a, type: python, script: scripts/mark.py, args: [a, "0.8"]}
  - {id: b, type: python, script: scripts/mark.py, args: [b, "0.8"]}
  - {id: c, type: python, script: scripts/mark.py, args: [c, "0.8"], needs: [a, b]}
""",
    )
    assert result.status == "success"
    a, b, c = intervals(graph, "a", "b", "c")
    assert max_concurrency([a, b]) == 2  # they overlap
    assert c[0] >= max(a[1], b[1]) - 0.05  # c waits for both


def test_max_parallel_limits_concurrency(graph: Path) -> None:
    steps = "\n".join(
        f'  - {{id: s{i}, type: python, script: scripts/mark.py, args: [s{i}, "0.5"]}}'
        for i in range(4)
    )
    run(graph, f"name: p\nmax_parallel: 2\nsteps:\n{steps}\n")
    spans = intervals(graph, "s0", "s1", "s2", "s3")
    assert max_concurrency(spans) == 2


# -- conditional edges ----------------------------------------------------------


def test_failure_branch_runs_only_when_the_step_fails(project: Path) -> None:
    body = """
name: p
steps:
  - {id: work, type: python, script: scripts/%s}
  - {id: alert, type: shell, command: echo alert, needs: [{step: work, when: failure}]}
  - {id: publish, type: shell, command: echo ok, needs: [{step: work, when: success}]}
"""
    ok = run(project, body % "ok.py")
    assert statuses(ok) == {"work": "success", "alert": "skipped", "publish": "success"}
    assert ok.status == "success"  # a skipped recovery branch is not a failure

    bad = run(project, body % "fail.py")
    assert statuses(bad) == {"work": "failed", "alert": "success", "publish": "skipped"}
    assert bad.status == "failed"  # decided: any real failure fails the run


def test_always_edge_runs_cleanup_even_with_fail_fast(project: Path) -> None:
    result = run(
        project,
        """
name: p
fail_fast: true
max_parallel: 1
steps:
  - {id: work, type: python, script: scripts/fail.py}
  - {id: other, type: python, script: scripts/ok.py}
  - {id: cleanup, type: shell, command: echo bye, needs: [{step: work, when: always}]}
""",
    )
    assert statuses(result) == {"work": "failed", "other": "skipped", "cleanup": "success"}


def test_always_edge_also_runs_after_a_skipped_step(project: Path) -> None:
    result = run(
        project,
        """
name: p
steps:
  - {id: work, type: python, script: scripts/ok.py}
  - {id: alert, type: shell, command: echo x, needs: [{step: work, when: failure}]}
  - {id: done, type: shell, command: echo done, needs: [{step: alert, when: always}]}
""",
    )
    assert statuses(result) == {"work": "success", "alert": "skipped", "done": "success"}


def test_ignore_failure_does_not_fail_the_run(project: Path) -> None:
    result = run(
        project,
        """
name: p
steps:
  - {id: flaky, type: python, script: scripts/fail.py, ignore_failure: true}
  - {id: fine, type: python, script: scripts/ok.py}
""",
    )
    assert statuses(result)["flaky"] == "failed"
    assert result.steps[0].ignored is True
    assert result.status == "success"


def test_ignored_failure_does_not_trigger_fail_fast(project: Path) -> None:
    result = run(
        project,
        """
name: p
fail_fast: true
max_parallel: 1
steps:
  - {id: flaky, type: python, script: scripts/fail.py, ignore_failure: true}
  - {id: next, type: python, script: scripts/ok.py}
""",
    )
    assert statuses(result) == {"flaky": "failed", "next": "success"}
