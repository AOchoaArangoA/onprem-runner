from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import yaml

from onprem_runner.config import execution_order, load_config
from onprem_runner.errors import ConfigError
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.models import ScheduleConfig, StepResult
from onprem_runner.runner import Runner, new_run_id
from onprem_runner.scheduler import (
    ScheduledTask,
    SchedulerBackend,
    SchedulerError,
    TaskScheduler,
    task_name,
)
from onprem_runner.store import HISTORY_DIRNAME, RunStore

log = logging.getLogger(LOGGER_NAME)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SKIP_DIRS = {"node_modules", "venv", "site-packages", "__pycache__", "dist", "build"}
MAX_DEPTH = 3
MAX_BODY = 4096
SCHEDULE_TTL = 20.0  # seconds between Task Scheduler queries


class BusyError(Exception):
    """The pipeline already has a run in progress."""


@dataclass
class PipelineInfo:
    file: Path
    rel: str
    name: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    schedule: ScheduleConfig | None = None

    @property
    def task(self) -> str:
        return task_name(self.file, self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.rel,
            "name": self.name,
            "steps": self.steps,
            "error": self.error,
        }


class ScheduleCache:
    """Task Scheduler state, refreshed in the background so /api/state never blocks."""

    def __init__(self, backend: SchedulerBackend, ttl: float = SCHEDULE_TTL) -> None:
        self.backend = backend
        self.ttl = ttl
        self._tasks: dict[str, ScheduledTask] | None = None
        self._error: str | None = None
        self._fetched = float("-inf")
        self._refreshing = False
        self._lock = threading.Lock()

    def get(self) -> tuple[dict[str, ScheduledTask] | None, str | None]:
        with self._lock:
            stale = time.monotonic() - self._fetched > self.ttl
            start = stale and not self._refreshing and self.backend.available
            if start:
                self._refreshing = True
        if start:
            threading.Thread(target=self.refresh, daemon=True).start()
        with self._lock:
            return self._tasks, self._error

    def refresh(self) -> None:
        try:
            tasks, error = self.backend.list_tasks(), None
        except SchedulerError as exc:
            tasks, error = None, str(exc)
        with self._lock:
            self._tasks, self._error = tasks, error
            self._fetched = time.monotonic()
            self._refreshing = False


class UIState:
    """Pipelines discovered under ``root``, plus the runs started from the UI."""

    def __init__(
        self,
        root: Path,
        token: str | None = None,
        scheduler: SchedulerBackend | None = None,
    ) -> None:
        self.root = root.resolve()
        self.scheduler = ScheduleCache(scheduler or TaskScheduler())
        self.token = token or secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, Any]] = {}

    # -- discovery ---------------------------------------------------------
    def discover(self) -> list[PipelineInfo]:
        found: list[PipelineInfo] = []
        for path in sorted([*self.root.rglob("*.yaml"), *self.root.rglob("*.yml")]):
            relative = path.relative_to(self.root)
            parts = relative.parts[:-1]
            if len(parts) > MAX_DEPTH or any(p.startswith(".") or p in SKIP_DIRS for p in parts):
                continue
            info = self._inspect(path, relative.as_posix())
            if info is not None:
                found.append(info)
        return found

    @staticmethod
    def _inspect(path: Path, rel: str) -> PipelineInfo | None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(raw, dict) or "steps" not in raw or "name" not in raw:
            return None  # not a pipeline file
        info = PipelineInfo(path, rel, str(raw["name"]))
        try:
            config = load_config(path, strict_env=False)
            Runner(config, path.parent)  # validates plugin parameters too
            info.schedule = config.schedule
            info.steps = [
                {
                    "id": s.id,
                    "type": s.type,
                    "needs": s.needs_ids,
                    "edges": [e.model_dump() for e in s.needs],
                }
                for s in execution_order(config.steps)
            ]
        except ConfigError as exc:
            info.error = str(exc)
        return info

    def find(self, rel: str) -> PipelineInfo | None:
        return next((p for p in self.discover() if p.rel == rel), None)

    # -- state -------------------------------------------------------------
    def snapshot(self, limit: int = 100) -> dict[str, Any]:
        pipelines = self.discover()
        runs: list[dict[str, Any]] = []
        for directory in {p.file.parent for p in pipelines}:
            runs.extend(RunStore(directory).recent(limit))
        runs.sort(key=lambda r: r["started_at"], reverse=True)
        runs = runs[:limit]
        for run in runs:
            run["file"] = self._relative(run.get("pipeline_file"))
            run.pop("pipeline_file", None)
        with self._lock:
            active = [dict(a, steps=dict(a["steps"])) for a in self._active.values()]
        tasks, schedule_error = self.scheduler.get()
        entries = []
        for p in pipelines:
            entry = p.to_dict()
            task = tasks.get(p.task) if tasks is not None else None
            entry["schedule"] = {
                "declared": p.schedule.model_dump() if p.schedule else None,
                "description": p.schedule.describe() if p.schedule else None,
                "task": task.to_dict() if task else None,
            }
            entries.append(entry)
        return {
            "root": str(self.root),
            "pipelines": entries,
            "runs": runs,
            "active": active,
            "scheduler": {
                "available": self.scheduler.backend.available,
                "loaded": tasks is not None,
                "error": schedule_error,
            },
        }

    def _relative(self, pipeline_file: str | None) -> str | None:
        if not pipeline_file:
            return None
        try:
            return Path(pipeline_file).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

    def run_detail(self, rel: str, run_id: str) -> dict[str, Any] | None:
        info = self.find(rel)
        if info is None:
            return None
        record = RunStore(info.file.parent).get(run_id)
        if record is not None:
            record["file"] = rel
            record.pop("pipeline_file", None)
        return record

    # -- scheduling --------------------------------------------------------
    def change_schedule(self, rel: str, action: str) -> None:
        info = self.find(rel)
        if info is None:
            raise ConfigError(f"unknown pipeline {rel!r}")
        backend = self.scheduler.backend
        try:
            if action == "install":
                if info.schedule is None:
                    raise ConfigError("this pipeline has no `schedule:` block")
                backend.install(info.file, info.name, info.schedule)
            elif action == "remove":
                backend.remove(info.file, info.name)
            else:
                raise ConfigError(f"unknown action {action!r}")
        except SchedulerError as exc:
            raise ConfigError(str(exc)) from exc
        self.scheduler.refresh()

    # -- running -----------------------------------------------------------
    def start_run(self, rel: str) -> str:
        info = self.find(rel)
        if info is None:
            raise ConfigError(f"unknown pipeline {rel!r}")
        config = load_config(info.file)
        run_id = new_run_id()

        def on_event(event: str, step_id: str, result: StepResult | None) -> None:
            with self._lock:
                steps = self._active[run_id]["steps"]
                steps[step_id] = (
                    "running" if event == "step_started" else (result.status if result else "?")
                )

        runner = Runner(
            config,
            info.file.parent,
            store=RunStore(info.file.parent),
            pipeline_file=info.file,
            on_event=on_event,
        )
        with self._lock:
            if any(a["file"] == rel for a in self._active.values()):
                raise BusyError(rel)
            self._active[run_id] = {
                "run_id": run_id,
                "file": rel,
                "pipeline": config.name,
                "started_at": datetime.now().isoformat(),
                "steps": {s.id: "pending" for s in config.steps},
            }

        def work() -> None:
            try:
                runner.run(run_id)
            except Exception:
                log.exception("run crashed")
            finally:
                with self._lock:
                    self._active.pop(run_id, None)

        threading.Thread(target=work, daemon=True, name=f"run-{run_id}").start()
        return run_id


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: UIState) -> None:
        super().__init__(address, Handler)
        self.state = state
        # Only enforce the Host check when bound to loopback (DNS-rebinding defence).
        self.restrict_hosts = address[0] in LOOPBACK_HOSTS


def create_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    scheduler: SchedulerBackend | None = None,
) -> UIServer:
    return UIServer((host, port), UIState(root, scheduler=scheduler))


class Handler(BaseHTTPRequestHandler):
    server_version = "onprem-runner-ui"

    @property
    def state(self) -> UIState:
        return cast(UIServer, self.server).state

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("ui %s", format % args)

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _host_allowed(self) -> bool:
        if not cast(UIServer, self.server).restrict_hosts:
            return True
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        return hostname in LOOPBACK_HOSTS

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._host_allowed():
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid host"})
            return
        url = urlparse(self.path)
        if url.path == "/":
            html = resources.files("onprem_runner.ui").joinpath("index.html").read_text("utf-8")
            self._send(
                HTTPStatus.OK,
                html.replace("__TOKEN__", self.state.token).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        elif url.path == "/api/state":
            self._json(HTTPStatus.OK, self.state.snapshot())
        elif url.path == "/api/run":
            query = parse_qs(url.query)
            detail = self.state.run_detail(query.get("file", [""])[0], query.get("id", [""])[0])
            if detail is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
            else:
                self._json(HTTPStatus.OK, detail)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid host"})
            return
        if not hmac.compare_digest(self.headers.get("X-OPR-Token", ""), self.state.token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
            return
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/schedule"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), MAX_BODY)
            body = json.loads(self.rfile.read(length) or b"{}")
            rel = str(body["file"])
        except (ValueError, KeyError, TypeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": 'expected JSON {"file": ...}'})
            return
        if path == "/api/schedule":
            try:
                self.state.change_schedule(rel, str(body.get("action", "")))
            except ConfigError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._json(HTTPStatus.OK, {"ok": True})
            return
        try:
            run_id = self.state.start_run(rel)
        except BusyError:
            self._json(HTTPStatus.CONFLICT, {"error": "this pipeline is already running"})
        except ConfigError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            self._json(HTTPStatus.ACCEPTED, {"run_id": run_id})


__all__ = ["HISTORY_DIRNAME", "BusyError", "UIServer", "UIState", "create_server"]
