from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import write_pipeline
from onprem_runner.config import load_config
from onprem_runner.errors import ConfigError
from onprem_runner.interpreters import detect, probe
from onprem_runner.runner import Runner
from onprem_runner.settings import Settings, SettingsStore
from onprem_runner.steps.python_step import EMBEDDED_FLAG, interpreter_command

# -- settings ------------------------------------------------------------------------


def test_defaults_when_there_is_no_file(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "settings.json").get()
    assert settings == Settings()
    assert settings.retention_days == 90
    assert settings.language == "auto"
    assert settings.close_to_tray is True


def test_update_persists_and_merges_partially(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "data" / "settings.json")
    store.update({"language": "es", "retention_days": 30})
    store.update({"theme": "dark"})  # must not reset the previous values
    reloaded = SettingsStore(store.path).get()
    assert (reloaded.language, reloaded.retention_days, reloaded.theme) == ("es", 30, "dark")


@pytest.mark.parametrize(
    "patch",
    [
        {"language": "fr"},
        {"retention_days": 0},
        {"retention_days": 99999},
        {"max_lines_per_run": 5},
        {"unknown_key": 1},
    ],
)
def test_invalid_updates_are_rejected_and_change_nothing(tmp_path: Path, patch: dict) -> None:  # type: ignore[type-arg]
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"language": "en"})
    with pytest.raises(ConfigError):
        store.update(patch)
    assert store.get().language == "en"


def test_a_damaged_settings_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    assert SettingsStore(path).get() == Settings()
    assert SettingsStore(path).update({"language": "es"}).language == "es"  # and it recovers


# -- interpreters ---------------------------------------------------------------------


def test_probe_reports_the_version_of_a_working_interpreter() -> None:
    version = probe(sys.executable)
    assert version is not None
    assert version.startswith(f"{sys.version_info.major}.{sys.version_info.minor}")


def test_probe_rejects_things_that_are_not_python(tmp_path: Path) -> None:
    assert probe(str(tmp_path / "nope.exe")) is None
    assert probe("cmd" if sys.platform == "win32" else "true") is None


def test_detect_finds_a_venv_inside_the_workspace(tmp_path: Path) -> None:
    venv = tmp_path / "proj" / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    found = detect(tmp_path)
    venvs = [i for i in found if i.source == "venv"]
    assert len(venvs) == 1
    assert Path(venvs[0].path).is_relative_to(venv)
    assert venvs[0].version is not None


def test_detect_has_no_duplicates() -> None:
    paths = [i.path.lower() for i in detect(None)]
    assert len(paths) == len(set(paths))


# -- choosing the interpreter for a step --------------------------------------------------


def test_interpreter_command_choices() -> None:
    assert interpreter_command("C:/Python/python.exe") == ["C:/Python/python.exe"]
    assert interpreter_command(None) == [sys.executable]
    assert interpreter_command("embedded") == [sys.executable]  # not frozen here


def test_embedded_script_mode_runs_a_script_with_arguments(tmp_path: Path) -> None:
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\nprint('name=', __name__, 'args=', sys.argv[1:])\n", encoding="utf-8"
    )
    out = subprocess.run(
        [sys.executable, "-m", "onprem_runner", EMBEDDED_FLAG, str(script), "a", "b"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "name= __main__ args= ['a', 'b']" in out


def test_default_python_is_used_when_the_step_does_not_choose(project: Path) -> None:
    (project / "scripts" / "who.py").write_text(
        "import sys\nprint(sys.executable)\n", encoding="utf-8"
    )
    config = load_config(
        write_pipeline(
            project, "name: p\nsteps:\n  - {id: s, type: python, script: scripts/who.py}\n"
        )
    )
    result = Runner(config, project, default_python=sys.executable).run()
    assert result.steps[0].status == "success"
    assert Path(result.steps[0].stdout.strip()).resolve() == Path(sys.executable).resolve()


def test_step_interpreter_overrides_the_default(project: Path) -> None:
    (project / "scripts" / "who.py").write_text("print('ran')\n", encoding="utf-8")
    config = load_config(
        write_pipeline(
            project,
            "name: p\nsteps:\n"
            f"  - {{id: s, type: python, script: scripts/who.py, python: '{sys.executable}'}}\n",
        )
    )
    result = Runner(config, project, default_python="C:/does/not/exist/python.exe").run()
    assert result.steps[0].status == "success"


def test_a_missing_interpreter_fails_the_step_with_a_readable_error(project: Path) -> None:
    config = load_config(
        write_pipeline(
            project, "name: p\nsteps:\n  - {id: s, type: python, script: scripts/ok.py}\n"
        )
    )
    result = Runner(config, project, default_python="C:/does/not/exist/python.exe").run()
    assert result.steps[0].status == "failed"
    assert result.steps[0].exit_code == -1
    assert result.steps[0].stderr.strip()
