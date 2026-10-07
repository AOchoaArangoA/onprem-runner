from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import write_pipeline
from onprem_runner.control import MAX_OUTPUT_CHARS, ControlError, PipelineControl

PIPELINE = "name: demo\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"


class NoScheduler:
    available = False

    def install(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("not expected")

    def remove(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("not expected")

    def list_tasks(self) -> dict[str, Any]:
        return {}


@pytest.fixture
def control(project: Path) -> PipelineControl:
    write_pipeline(project, PIPELINE)
    return PipelineControl(project, NoScheduler())


def test_list_pipelines_before_any_run(control: PipelineControl) -> None:
    [entry] = control.list_pipelines()
    assert entry["file"] == "pipeline.yaml"
    assert entry["steps"] == ["a"]
    assert entry["last_run"] is None
    assert entry["schedule"] is None


def test_run_and_wait_returns_result_with_output(control: PipelineControl) -> None:
    result = control.run_pipeline("pipeline.yaml", wait_seconds=30)
    assert result["status"] == "success"
    assert result["steps"][0]["stdout"].strip() == "ok"
    [entry] = control.list_pipelines()
    assert entry["last_run"]["run_id"] == result["run_id"]


def test_run_without_waiting_returns_run_id_then_get_run(control: PipelineControl) -> None:
    started = control.run_pipeline("pipeline.yaml")
    assert started["status"] == "running"
    for _ in range(100):
        detail = control.get_run("pipeline.yaml", started["run_id"])
        if detail["status"] != "running":
            break
        import time

        time.sleep(0.1)
    assert detail["status"] == "success"


def test_list_runs_filters_and_limits(project: Path) -> None:
    write_pipeline(
        project,
        "name: demo\nsteps:\n  - {id: a, type: python, script: scripts/fail.py}\n",
    )
    control = PipelineControl(project, NoScheduler())
    for _ in range(3):
        control.run_pipeline("pipeline.yaml", wait_seconds=30)
    assert len(control.list_runs(limit=2)) == 2
    assert len(control.list_runs(status="failed")) == 3
    assert control.list_runs(status="success") == []
    with pytest.raises(ControlError):
        control.list_runs(status="weird")


def test_long_output_is_truncated_keeping_the_tail(project: Path) -> None:
    (project / "scripts" / "noisy.py").write_text(
        f'print("x" * {MAX_OUTPUT_CHARS * 3})\nprint("THE-END")\n', encoding="utf-8"
    )
    write_pipeline(
        project, "name: demo\nsteps:\n  - {id: a, type: python, script: scripts/noisy.py}\n"
    )
    result = PipelineControl(project, NoScheduler()).run_pipeline("pipeline.yaml", wait_seconds=30)
    stdout = result["steps"][0]["stdout"]
    assert "chars omitted" in stdout
    assert "THE-END" in stdout
    assert len(stdout) < MAX_OUTPUT_CHARS + 100


def test_errors_are_readable(control: PipelineControl) -> None:
    with pytest.raises(ControlError, match="unknown pipeline"):
        control.run_pipeline("../secret.yaml")
    with pytest.raises(ControlError, match="not found"):
        control.get_run("pipeline.yaml", "20260101-000000-abcdef")
    with pytest.raises(ControlError, match="no `schedule:` block"):
        control.set_schedule("pipeline.yaml", "install")


def test_mcp_server_end_to_end_over_stdio(control: PipelineControl, project: Path) -> None:
    """Launch `opr mcp` as a child process and talk to it like Claude Code would."""
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario() -> tuple[list[str], bool, str, bool]:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "onprem_runner", "mcp", str(project)]
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            ran = await session.call_tool(
                "run_pipeline", {"file": "pipeline.yaml", "wait_seconds": 30}
            )
            bad = await session.call_tool("run_pipeline", {"file": "../nope.yaml"})
            return names, bool(ran.is_error), str(ran.content), bool(bad.is_error)

    names, ran_error, ran_text, bad_error = asyncio.run(scenario())
    assert set(names) == {"list_pipelines", "list_runs", "get_run", "run_pipeline", "set_schedule"}
    assert not ran_error
    assert '"status": "success"' in ran_text
    assert bad_error
