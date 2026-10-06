from __future__ import annotations

import json
import threading
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from conftest import write_pipeline
from onprem_runner.cli import app
from onprem_runner.models import ScheduleConfig
from onprem_runner.scheduler import (
    ScheduledTask,
    SchedulerError,
    build_task_xml,
    task_name,
)
from onprem_runner.ui import UIServer, create_server

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
NOW = datetime(2026, 10, 5, 12, 0, 0)
PIPELINE = (
    "name: demo\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"
    "schedule: {frequency: daily, at: '08:30'}\n"
)


def xml_for(schedule: dict[str, Any], path: Path, **kwargs: Any) -> ET.Element:
    return ET.fromstring(build_task_xml(ScheduleConfig(**schedule), path, now=NOW, **kwargs))


@pytest.mark.parametrize(
    "schedule",
    [
        {"frequency": "daily"},
        {"frequency": "daily", "at": "8:30"},
        {"frequency": "daily", "at": "25:00"},
        {"frequency": "weekly", "at": "08:00"},
        {"frequency": "daily", "at": "08:00", "days": ["MON"]},
        {"frequency": "minutes", "at": "08:00"},
        {"frequency": "minutes", "every": 0},
        {"frequency": "hourly", "every": 24},
        {"frequency": "startup", "every": 2},
        {"frequency": "yearly"},
    ],
)
def test_invalid_schedules_are_rejected(schedule: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScheduleConfig(**schedule)


def test_unquoted_time_gets_a_helpful_error(project: Path) -> None:
    from onprem_runner.config import load_config
    from onprem_runner.errors import ConfigError

    path = write_pipeline(
        project,
        "name: p\nsteps:\n  - {id: a, type: shell, command: x}\n"
        "schedule: {frequency: daily, at: 8:30}\n",  # YAML reads 8:30 as 510
    )
    with pytest.raises(ConfigError, match="quote the time"):
        load_config(path)


@pytest.mark.parametrize(
    ("schedule", "text"),
    [
        ({"frequency": "daily", "at": "08:30"}, "daily at 08:30"),
        ({"frequency": "minutes", "every": 15}, "every 15 minutes"),
        (
            {"frequency": "weekly", "days": ["MON", "FRI"], "at": "02:00"},
            "weekly on MON,FRI at 02:00",
        ),
        ({"frequency": "startup"}, "at system startup"),
    ],
)
def test_describe(schedule: dict[str, Any], text: str) -> None:
    assert ScheduleConfig(**schedule).describe() == text


def test_daily_xml(tmp_path: Path) -> None:
    root = xml_for({"frequency": "daily", "at": "08:30", "every": 2}, tmp_path / "p.yaml")
    assert root.findtext(".//t:StartBoundary", namespaces=NS) == "2026-10-05T08:30:00"
    assert root.findtext(".//t:DaysInterval", namespaces=NS) == "2"
    assert root.findtext(".//t:Arguments", namespaces=NS) == (
        f'-m onprem_runner run "{(tmp_path / "p.yaml").resolve()}"'
    )
    assert root.findtext(".//t:WorkingDirectory", namespaces=NS) == str(tmp_path.resolve())


def test_weekly_xml_lists_days(tmp_path: Path) -> None:
    root = xml_for(
        {"frequency": "weekly", "days": ["MON", "FRI"], "at": "02:00"}, tmp_path / "p.yaml"
    )
    days = root.find(".//t:DaysOfWeek", NS)
    assert days is not None
    assert [child.tag.split("}")[1] for child in days] == ["Monday", "Friday"]


def test_repeating_xml_uses_repetition(tmp_path: Path) -> None:
    root = xml_for({"frequency": "minutes", "every": 15}, tmp_path / "p.yaml")
    assert root.findtext(".//t:Repetition/t:Interval", namespaces=NS) == "PT15M"
    hourly = xml_for({"frequency": "hourly", "every": 2}, tmp_path / "p.yaml")
    assert hourly.findtext(".//t:Repetition/t:Interval", namespaces=NS) == "PT2H"


def test_startup_and_system_xml(tmp_path: Path) -> None:
    root = xml_for({"frequency": "startup"}, tmp_path / "p.yaml", system=True)
    assert root.find(".//t:BootTrigger", NS) is not None
    assert root.findtext(".//t:Principal/t:UserId", namespaces=NS) == "S-1-5-18"


def test_xml_escapes_special_characters(tmp_path: Path) -> None:
    path = tmp_path / "a&b <c>.yaml"
    root = xml_for({"frequency": "daily", "at": "01:00"}, path)
    assert str(path.resolve()) in (root.findtext(".//t:Arguments", namespaces=NS) or "")


def test_task_name_is_stable_and_unique_per_path(tmp_path: Path) -> None:
    a, b = tmp_path / "x" / "p.yaml", tmp_path / "y" / "p.yaml"
    assert task_name(a, "My Report!") == task_name(a, "My Report!")
    assert task_name(a, "My Report!") != task_name(b, "My Report!")
    assert task_name(a, "My Report!").startswith("My-Report-")


def test_cli_dry_run_prints_xml_without_touching_the_system(project: Path) -> None:
    pipeline = write_pipeline(project, PIPELINE)
    result = CliRunner().invoke(app, ["schedule", "install", str(pipeline), "--dry-run"])
    assert result.exit_code == 0
    assert "<StartBoundary>" in result.output


def test_cli_install_requires_schedule_block(project: Path) -> None:
    pipeline = write_pipeline(project, "name: p\nsteps:\n  - {id: a, type: shell, command: x}\n")
    result = CliRunner().invoke(app, ["schedule", "install", str(pipeline)])
    assert result.exit_code == 2
    assert "no `schedule:` block" in result.output


# -- dashboard integration with a fake backend --------------------------------


class FakeScheduler:
    available = True

    def __init__(self) -> None:
        self.tasks: dict[str, ScheduledTask] = {}
        self.fail = False

    def install(self, pipeline_file: Path, name: str, schedule: ScheduleConfig, **_: Any) -> str:
        if self.fail:
            raise SchedulerError("Acceso denegado")
        key = task_name(pipeline_file, name)
        self.tasks[key] = ScheduledTask(key, "Ready", "2026-10-06T08:30:00", None, 267011)
        return key

    def remove(self, pipeline_file: Path, name: str) -> bool:
        return self.tasks.pop(task_name(pipeline_file, name), None) is not None

    def list_tasks(self) -> dict[str, ScheduledTask]:
        return dict(self.tasks)


@pytest.fixture
def fake() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def server(project: Path, fake: FakeScheduler) -> Iterator[UIServer]:
    write_pipeline(project, PIPELINE)
    srv = create_server(project, "127.0.0.1", 0, scheduler=fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def post(server: UIServer, body: dict[str, Any], token: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/api/schedule",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"X-OPR-Token": token or server.state.token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def schedule_of(server: UIServer) -> dict[str, Any]:
    server.state.scheduler.refresh()
    return server.state.snapshot()["pipelines"][0]["schedule"]


def test_snapshot_reports_declared_and_installed_schedule(server: UIServer) -> None:
    info = schedule_of(server)
    assert info["declared"]["frequency"] == "daily"
    assert info["description"] == "daily at 08:30"
    assert info["task"] is None
    assert server.state.snapshot()["scheduler"]["available"] is True


def test_install_and_remove_through_the_api(server: UIServer) -> None:
    assert post(server, {"file": "pipeline.yaml", "action": "install"})[0] == 200
    assert schedule_of(server)["task"]["state"] == "Ready"
    assert post(server, {"file": "pipeline.yaml", "action": "remove"})[0] == 200
    assert schedule_of(server)["task"] is None


def test_schedule_api_requires_token_and_valid_input(server: UIServer) -> None:
    assert post(server, {"file": "pipeline.yaml", "action": "install"}, token="nope")[0] == 403
    assert post(server, {"file": "../x.yaml", "action": "install"})[0] == 400
    assert post(server, {"file": "pipeline.yaml", "action": "explode"})[0] == 400


def test_backend_errors_reach_the_user(server: UIServer, fake: FakeScheduler) -> None:
    fake.fail = True
    status, body = post(server, {"file": "pipeline.yaml", "action": "install"})
    assert status == 400
    assert "Acceso denegado" in body["error"]
