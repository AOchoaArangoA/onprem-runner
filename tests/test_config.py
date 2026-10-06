from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_pipeline
from onprem_runner.config import execution_order, load_config
from onprem_runner.errors import ConfigError


def test_loads_valid_pipeline(project: Path) -> None:
    path = write_pipeline(
        project,
        """
name: demo
steps:
  - {id: a, type: python, script: scripts/ok.py}
  - {id: b, type: python, script: scripts/ok.py, needs: [a]}
""",
    )
    config = load_config(path)
    assert config.name == "demo"
    assert [s.id for s in execution_order(config.steps)] == ["a", "b"]


def test_order_respects_dependencies(project: Path) -> None:
    path = write_pipeline(
        project,
        """
name: demo
steps:
  - {id: b, type: shell, command: x, needs: [a]}
  - {id: a, type: shell, command: x}
""",
    )
    assert [s.id for s in execution_order(load_config(path).steps)] == ["a", "b"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("name: x\nsteps: []\n", "invalid pipeline"),
        ("- just\n- a list\n", "mapping"),
        (
            "name: x\nsteps:\n  - {id: a, type: shell, command: x}\n"
            "  - {id: a, type: shell, command: x}\n",
            "duplicate",
        ),
        ("name: x\nsteps:\n  - {id: a, type: shell, command: x, needs: [z]}\n", "unknown step"),
        (
            "name: x\nsteps:\n  - {id: a, type: shell, command: x, needs: [b]}\n"
            "  - {id: b, type: shell, command: x, needs: [a]}\n",
            "cycle",
        ),
    ],
)
def test_rejects_invalid_pipeline(project: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_pipeline(project, body))


def test_env_expansion(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_CMD", "echo hi")
    path = write_pipeline(
        project, "name: x\nsteps:\n  - {id: a, type: shell, command: '${MY_CMD}'}\n"
    )
    assert load_config(path).steps[0].params["command"] == "echo hi"


def test_missing_env_is_error_unless_lenient(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOPE_VAR", raising=False)
    path = write_pipeline(
        project, "name: x\nsteps:\n  - {id: a, type: shell, command: '${NOPE_VAR}'}\n"
    )
    with pytest.raises(ConfigError, match="NOPE_VAR"):
        load_config(path)
    assert load_config(path, strict_env=False).steps[0].params["command"] == "${NOPE_VAR}"
