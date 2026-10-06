from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from conftest import write_pipeline
from onprem_runner.cli import app

runner = CliRunner()


def test_init_then_validate_then_run(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    pipeline = str(tmp_path / "pipeline.yaml")
    assert runner.invoke(app, ["validate", pipeline]).exit_code == 0
    assert runner.invoke(app, ["run", pipeline]).exit_code == 0
    assert list((tmp_path / "out").glob("*.json"))


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path)])
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 2
    assert runner.invoke(app, ["init", str(tmp_path), "--force"]).exit_code == 0


def test_run_exit_codes(project: Path) -> None:
    failing = write_pipeline(
        project, "name: p\nsteps:\n  - {id: a, type: python, script: scripts/fail.py}\n"
    )
    assert runner.invoke(app, ["run", str(failing)]).exit_code == 1
    broken = write_pipeline(project, "name: p\nsteps: []\n")
    assert runner.invoke(app, ["run", str(broken)]).exit_code == 2


def test_log_file_is_json_lines(project: Path) -> None:
    pipeline = write_pipeline(
        project, "name: p\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"
    )
    log_file = project / "logs" / "run.jsonl"
    runner.invoke(app, ["run", str(pipeline), "--log-file", str(log_file)])
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert any('"event": "run finished"' in line for line in lines)
