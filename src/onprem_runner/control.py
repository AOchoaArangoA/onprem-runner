"""Programmatic control surface shared by remote front-ends (currently the MCP server).

Every method returns plain JSON-serialisable data and raises :class:`ControlError`
with a message meant to be shown to the caller.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from onprem_runner.errors import ConfigError
from onprem_runner.scheduler import SchedulerBackend
from onprem_runner.ui.server import BusyError, UIState

MAX_OUTPUT_CHARS = 4000  # per stream; the tail is kept because errors are usually last
MAX_WAIT_SECONDS = 300


class ControlError(Exception):
    """A request that cannot be fulfilled (unknown pipeline, busy, invalid state...)."""


def _tail(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return f"[... {len(text) - MAX_OUTPUT_CHARS} chars omitted ...]\n" + text[-MAX_OUTPUT_CHARS:]


class PipelineControl:
    """Discover, run, inspect and schedule the pipelines under ``root``."""

    def __init__(self, root: Path, scheduler: SchedulerBackend | None = None) -> None:
        self.state = UIState(root, scheduler=scheduler)

    # -- read -----------------------------------------------------------------
    def list_pipelines(self) -> list[dict[str, Any]]:
        """Pipelines found under the project directory, with last run and schedule."""
        if self.state.scheduler.backend.available:
            self.state.scheduler.refresh()
        snapshot = self.state.snapshot()
        runs = snapshot["runs"]
        active = {a["file"]: a for a in snapshot["active"]}
        result = []
        for p in snapshot["pipelines"]:
            last = next((r for r in runs if r["file"] == p["file"]), None)
            schedule = p["schedule"]
            task = schedule["task"]
            result.append(
                {
                    "file": p["file"],
                    "name": p["name"],
                    "steps": [s["id"] for s in p["steps"]],
                    "config_error": p["error"],
                    "running_run_id": active[p["file"]]["run_id"] if p["file"] in active else None,
                    "last_run": _summary(last) if last else None,
                    "schedule": schedule["description"],
                    "schedule_installed": task is not None,
                    "next_run": task["next_run"] if task else None,
                }
            )
        return result

    def list_runs(
        self, file: str | None = None, status: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Most recent runs first, optionally filtered by pipeline file and status."""
        if status not in (None, "success", "failed"):
            raise ControlError("status must be 'success' or 'failed'")
        runs = self.state.snapshot(limit=200)["runs"]
        selected = [
            r
            for r in runs
            if (file is None or r["file"] == file) and (status is None or r["status"] == status)
        ]
        return [_summary(r) for r in selected[: max(1, min(limit, 50))]]

    def get_run(self, file: str, run_id: str) -> dict[str, Any]:
        """Full detail of one run, including each step's stdout/stderr (truncated)."""
        detail = self.state.run_detail(file, run_id)
        if detail is None:
            active = [a for a in self.state.snapshot()["active"] if a["run_id"] == run_id]
            if active:
                return {
                    "run_id": run_id,
                    "file": file,
                    "status": "running",
                    "steps": active[0]["steps"],
                }
            raise ControlError(f"run {run_id!r} of {file!r} not found")
        for step in detail["steps"]:
            step["stdout"] = _tail(step.get("stdout", ""))
            step["stderr"] = _tail(step.get("stderr", ""))
        return detail

    # -- act ------------------------------------------------------------------
    def run_pipeline(self, file: str, wait_seconds: int = 0) -> dict[str, Any]:
        """Start a pipeline. With ``wait_seconds`` > 0, wait for it and return its result."""
        try:
            run_id = self.state.start_run(file)
        except BusyError as exc:
            raise ControlError(f"{file} is already running") from exc
        except ConfigError as exc:
            raise ControlError(str(exc)) from exc
        wait = max(0, min(wait_seconds, MAX_WAIT_SECONDS))
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not any(a["run_id"] == run_id for a in self.state.snapshot()["active"]):
                return self.get_run(file, run_id)
            time.sleep(0.5)
        return {
            "run_id": run_id,
            "file": file,
            "status": "running" if wait == 0 else "still_running",
        }

    def set_schedule(self, file: str, action: str) -> dict[str, Any]:
        """Install or remove the Windows scheduled task declared in the pipeline's YAML."""
        try:
            self.state.change_schedule(file, action)
        except ConfigError as exc:
            raise ControlError(str(exc)) from exc
        return next(p for p in self.list_pipelines() if p["file"] == file)


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "file": run["file"],
        "pipeline": run["pipeline"],
        "status": run["status"],
        "started_at": run["started_at"],
        "duration_s": run["duration"],
        "steps": {s["id"]: s["status"] for s in run["steps"]},
    }
