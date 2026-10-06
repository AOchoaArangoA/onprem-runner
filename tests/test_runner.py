from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_pipeline
from onprem_runner.config import load_config
from onprem_runner.errors import ConfigError
from onprem_runner.runner import Runner


def run_pipeline(project: Path, body: str):  # type: ignore[no-untyped-def]
    return Runner(load_config(write_pipeline(project, body)), project).run()


def statuses(result) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {s.id: s.status for s in result.steps}


def test_successful_run_captures_output(project: Path) -> None:
    result = run_pipeline(
        project, "name: p\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"
    )
    assert result.status == "success"
    assert result.steps[0].stdout.strip() == "ok"


def test_failure_skips_dependents_and_fail_fast(project: Path) -> None:
    result = run_pipeline(
        project,
        """
name: p
steps:
  - {id: bad, type: python, script: scripts/fail.py}
  - {id: after, type: python, script: scripts/ok.py, needs: [bad]}
  - {id: other, type: python, script: scripts/ok.py}
""",
    )
    assert result.status == "failed"
    assert statuses(result) == {"bad": "failed", "after": "skipped", "other": "skipped"}
    assert result.steps[0].exit_code == 3
    assert "boom" in result.steps[0].stderr


def test_without_fail_fast_independent_steps_still_run(project: Path) -> None:
    result = run_pipeline(
        project,
        """
name: p
fail_fast: false
steps:
  - {id: bad, type: python, script: scripts/fail.py}
  - {id: after, type: python, script: scripts/ok.py, needs: [bad]}
  - {id: other, type: python, script: scripts/ok.py}
""",
    )
    assert statuses(result) == {"bad": "failed", "after": "skipped", "other": "success"}


def test_timeout(project: Path) -> None:
    result = run_pipeline(
        project,
        "name: p\nsteps:\n  - {id: s, type: python, script: scripts/slow.py, timeout: 0.5}\n",
    )
    assert statuses(result) == {"s": "timeout"}


def test_retries_are_counted(project: Path) -> None:
    result = run_pipeline(
        project,
        "name: p\nsteps:\n  - {id: bad, type: python, script: scripts/fail.py, retries: 2}\n",
    )
    assert result.steps[0].attempts == 3


def test_missing_script_fails_step(project: Path) -> None:
    result = run_pipeline(
        project, "name: p\nsteps:\n  - {id: s, type: python, script: scripts/nope.py}\n"
    )
    assert statuses(result) == {"s": "failed"}
    assert "not found" in result.steps[0].stderr


def test_shell_step(project: Path) -> None:
    result = run_pipeline(project, "name: p\nsteps:\n  - {id: s, type: shell, command: echo hi}\n")
    assert result.status == "success"
    assert "hi" in result.steps[0].stdout


@pytest.mark.parametrize(
    "step",
    [
        "{id: s, type: nope}",
        "{id: s, type: python}",
        "{id: s, type: python, script: x.py, typo: 1}",
    ],
)
def test_bad_step_config_fails_before_running(project: Path, step: str) -> None:
    config = load_config(write_pipeline(project, f"name: p\nsteps:\n  - {step}\n"))
    with pytest.raises(ConfigError):
        Runner(config, project)


def test_file_notifier_respects_when(project: Path) -> None:
    body = """
name: p
steps:
  - {id: a, type: python, script: scripts/ok.py}
notifiers:
  - {type: file, path: 'out/{run_id}.json', when: failure}
  - {type: file, path: 'out/all-{run_id}.csv', format: csv}
"""
    result = run_pipeline(project, body)
    assert not list((project / "out").glob("*.json"))
    csv_file = project / "out" / f"all-{result.run_id}.csv"
    assert csv_file.read_text(encoding="utf-8").splitlines()[1].startswith("a,python,success")


def test_json_output_content(project: Path) -> None:
    result = run_pipeline(
        project,
        "name: p\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"
        "notifiers:\n  - {type: file, path: 'out/{run_id}.json'}\n",
    )
    data = json.loads((project / "out" / f"{result.run_id}.json").read_text(encoding="utf-8"))
    assert data["status"] == "success"
    assert data["steps"][0]["id"] == "a"
