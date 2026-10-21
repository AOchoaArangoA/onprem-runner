"""The engine: sole owner of the database, secrets and running pipelines.

Front-ends (the Electron app, and later the CLI and MCP) talk to it through the HTTP API in
:mod:`onprem_runner.engine.api`; nothing else opens the DuckDB file.
"""

from __future__ import annotations

import csv
import io
import logging
import queue
import shutil
import threading
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from onprem_runner import interpreters
from onprem_runner.config import load_config
from onprem_runner.db import Database
from onprem_runner.errors import ConfigError
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.logs import LogLine
from onprem_runner.models import StepResult
from onprem_runner.runner import Runner, new_run_id
from onprem_runner.secrets_store import Cipher, SecretError, SecretStore, referenced_secrets
from onprem_runner.settings import Settings, SettingsStore
from onprem_runner.store import RunStore
from onprem_runner.yamlio import (
    ConflictError as FileConflictError,
)
from onprem_runner.yamlio import (
    apply_graph,
    create_pipeline,
    load_graph,
    save_graph,
    validate_text,
)

log = logging.getLogger(LOGGER_NAME)

SKIP_DIRS = {"node_modules", "venv", "site-packages", "__pycache__", "dist", "build"}
MAX_DEPTH = 3
SUBSCRIBER_QUEUE_SIZE = 5000
EXPORT_PAGE = 5000


class EngineError(Exception):
    status = 500


class BadRequest(EngineError):
    status = 400


class NotFound(EngineError):
    status = 404


class Conflict(EngineError):
    status = 409


# -- live events ------------------------------------------------------------------------------


class Subscriber:
    """A consumer of live events. If it falls behind it is told to resync instead of growing
    without bound."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._lagged = False

    def offer(self, event: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._lagged = True

    def get(self, timeout: float) -> dict[str, Any] | None:
        if self._lagged:
            self._lagged = False
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            return {"type": "resync"}
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber()
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.offer(event)


# -- active runs ---------------------------------------------------------------------------------


@dataclass
class ActiveRun:
    run_id: str
    file: str
    pipeline: str
    trigger: str
    runner: Runner
    started_at: datetime = field(default_factory=datetime.now)
    steps: dict[str, str] = field(default_factory=dict)
    thread: threading.Thread | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "file": self.file,
            "pipeline": self.pipeline,
            "trigger": self.trigger,
            "started_at": self.started_at.isoformat(),
            "steps": dict(self.steps),
        }


class Engine:
    def __init__(
        self,
        data_dir: Path,
        *,
        workspace: Path | None = None,
        cipher: Cipher | None = None,
    ) -> None:
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_store = SettingsStore(data_dir / "settings.json")
        if workspace is not None:
            self.settings_store.update({"workspace": str(workspace)})
        settings = self.settings_store.get()
        self.db = Database(data_dir / "opr.duckdb", max_lines_per_run=settings.max_lines_per_run)
        self.secrets = SecretStore(data_dir / "secrets.json", cipher)
        self.bus = EventBus()
        self._lock = threading.Lock()
        self._active: dict[str, ActiveRun] = {}
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> dict[str, int]:
        """Housekeeping at startup: fail orphaned runs, import runs made while closed, purge."""
        interrupted = self.db.mark_interrupted()
        ingested = sum(self.db.ingest_spool(d) for d in {p.parent for p in self._pipeline_files()})
        purged = self.db.purge(self.settings.retention_days)["runs"]
        return {"interrupted": interrupted, "ingested": ingested, "purged": purged}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            runs = list(self._active.values())
        for run in runs:
            run.runner.cancel()
        for run in runs:
            if run.thread is not None:
                run.thread.join(timeout=15)
        self.db.close()

    # -- settings -------------------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self.settings_store.get()

    @property
    def workspace(self) -> Path:
        configured = self.settings.workspace
        path = Path(configured) if configured else self.data_dir / "pipelines"
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def update_settings(self, patch: dict[str, Any]) -> Settings:
        if patch.get("workspace"):
            target = Path(str(patch["workspace"]))
            if not target.is_dir():
                raise BadRequest(f"{target} is not an existing folder")
        try:
            settings = self.settings_store.update(patch)
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc
        self.db.max_lines_per_run = settings.max_lines_per_run
        self.bus.publish({"type": "settings_changed"})
        if "workspace" in patch:
            self.bus.publish({"type": "pipelines_changed"})
        return settings

    def interpreters(self) -> list[dict[str, str | None]]:
        return [i.to_dict() for i in interpreters.detect(self.workspace)]

    def check_interpreter(self, path: str) -> dict[str, Any]:
        version = interpreters.probe(path)
        return {"path": path, "ok": version is not None, "version": version}

    # -- pipelines -------------------------------------------------------------------------------
    def _pipeline_files(self) -> list[Path]:
        root = self.workspace
        files: list[Path] = []
        for path in sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")]):
            parts = path.relative_to(root).parts[:-1]
            if len(parts) > MAX_DEPTH or any(p.startswith(".") or p in SKIP_DIRS for p in parts):
                continue
            files.append(path)
        return files

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace).as_posix()

    def resolve(self, rel: str) -> Path:
        """Map a pipeline identifier from a client to a file inside the workspace."""
        if not rel or chr(0) in rel:
            raise NotFound("pipeline not found")
        root = self.workspace
        candidate = (root / rel).resolve()
        if (
            not candidate.is_relative_to(root)
            or candidate.suffix.lower() not in (".yaml", ".yml")
            or not candidate.is_file()
        ):
            raise NotFound("pipeline not found")
        return candidate

    def list_pipelines(self) -> list[dict[str, Any]]:
        with self._lock:
            active = {a.file: a.run_id for a in self._active.values()}
        result: list[dict[str, Any]] = []
        for path in self._pipeline_files():
            rel = self._rel(path)
            entry: dict[str, Any] = {
                "file": rel,
                "name": path.stem,
                "steps": 0,
                "schedule": None,
                "error": None,
                "last_run": None,
                "running_run_id": active.get(rel),
                "secrets_missing": [],
            }
            try:
                graph = load_graph(path, self.workspace)
            except ConfigError as exc:
                if not _looks_like_pipeline(path):
                    continue  # some other YAML file in the folder
                entry["error"] = str(exc)
            else:
                entry.update(
                    name=graph["name"], steps=len(graph["steps"]), schedule=graph["schedule"]
                )
                try:  # unknown step types or bad parameters: shown with an error, still editable
                    validate_text(path.read_text(encoding="utf-8"), path.parent)
                except ConfigError as exc:
                    entry["error"] = str(exc)
                entry["secrets_missing"] = sorted(
                    referenced_secrets([s["params"] for s in graph["steps"]] + graph["notifiers"])
                    - set(self._secret_names())
                )
            runs = self.db.list_runs(pipeline_file=str(path), limit=1)
            if runs:
                entry["last_run"] = self._public_run(runs[0])
            result.append(entry)
        return result

    def get_graph(self, rel: str) -> dict[str, Any]:
        path = self.resolve(rel)
        try:
            return load_graph(path, self.workspace)
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc

    def validate_graph(self, rel: str, graph: dict[str, Any]) -> dict[str, Any]:
        """Check a graph as the editor would save it, without writing anything."""
        path = self.resolve(rel)
        try:
            text = apply_graph(path.read_text(encoding="utf-8"), graph)
            validate_text(text, path.parent)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"malformed graph: {exc!r}"}
        return {"ok": True, "error": None}

    def save_graph(
        self, rel: str, graph: dict[str, Any], *, etag: str | None, force: bool = False
    ) -> dict[str, Any]:
        path = self.resolve(rel)
        try:
            new_etag = save_graph(
                path,
                graph,
                expected_etag=etag,
                backups_dir=path.parent / ".opr" / "backups",
                force=force,
            )
        except FileConflictError as exc:
            raise Conflict(str(exc)) from exc
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise BadRequest(f"malformed graph: {exc!r}") from exc
        self.bus.publish({"type": "pipelines_changed"})
        return {"etag": new_etag}

    def create_pipeline(self, name: str) -> dict[str, Any]:
        try:
            path = create_pipeline(self.workspace, name)
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc
        self.bus.publish({"type": "pipelines_changed"})
        return {"file": self._rel(path)}

    def delete_pipeline(self, rel: str) -> None:
        """Move the file to ``data/trash`` (recoverable) instead of deleting it."""
        path = self.resolve(rel)
        with self._lock:
            if any(a.file == rel for a in self._active.values()):
                raise Conflict("this pipeline is running")
        trash = self.data_dir / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), trash / f"{datetime.now():%Y%m%d-%H%M%S}-{path.name}")
        self.bus.publish({"type": "pipelines_changed"})

    # -- running ---------------------------------------------------------------------------------
    def start_run(
        self, rel: str, *, only: Collection[str] | None = None, trigger: str = "manual"
    ) -> str:
        path = self.resolve(rel)
        run_id = new_run_id()
        holder: dict[str, ActiveRun] = {}

        def on_event(event: str, step_id: str, result: StepResult | None) -> None:
            status = "running" if event == "step_started" else (result.status if result else "?")
            active = holder.get(run_id)
            if active is not None:
                active.steps[step_id] = status
            self.bus.publish(
                {
                    "type": "step",
                    "run_id": run_id,
                    "file": rel,
                    "step_id": step_id,
                    "status": status,
                    "exit_code": result.exit_code if result else None,
                    "duration": round(result.duration, 3) if result else None,
                }
            )

        def on_log(line: LogLine) -> None:
            self.bus.publish({"type": "log", **line.to_dict()})

        try:
            config = load_config(path)
            runner = Runner(
                config,
                path.parent,
                pipeline_file=path,
                on_event=on_event,
                on_log=on_log,
                recorder=self.db,
                store=RunStore(path.parent),
                secrets=self.secrets,
                default_python=self.settings.default_python,
            )
            if only is not None:
                known = {s.id for s in config.steps}
                if not set(only) <= known:
                    raise ConfigError(f"unknown steps: {sorted(set(only) - known)}")
        except (ConfigError, SecretError) as exc:
            raise BadRequest(str(exc)) from exc

        active = ActiveRun(
            run_id=run_id,
            file=rel,
            pipeline=config.name,
            trigger=trigger,
            runner=runner,
            steps={s.id: "pending" for s in config.steps if only is None or s.id in only},
        )
        with self._lock:
            if self._closed:
                raise Conflict("the engine is shutting down")
            if any(a.file == rel for a in self._active.values()):
                raise Conflict("this pipeline is already running")
            self._active[run_id] = active
        holder[run_id] = active

        def work() -> None:
            status = "failed"
            try:
                status = runner.run(run_id, trigger=trigger, only=only).status
            except Exception:
                log.exception("run %s crashed", run_id)
                self.db.fail_run(run_id)
            finally:
                self.db.flush()  # clients react to run_finished by reading the stored logs
                with self._lock:
                    self._active.pop(run_id, None)
                self.bus.publish(
                    {"type": "run_finished", "run_id": run_id, "file": rel, "status": status}
                )

        active.thread = threading.Thread(target=work, name=f"run-{run_id}", daemon=True)
        self.bus.publish({"type": "run_started", **active.to_dict()})
        active.thread.start()
        return run_id

    def cancel_run(self, run_id: str) -> None:
        with self._lock:
            active = self._active.get(run_id)
        if active is None:
            raise NotFound("that run is not active")
        active.runner.cancel()

    def active_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a.to_dict() for a in self._active.values()]

    # -- history and logs -----------------------------------------------------------------------
    def _public_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run = dict(run)
        pipeline_file = run.pop("pipeline_file", None)
        run["file"] = None
        if pipeline_file:
            try:
                run["file"] = self._rel(Path(pipeline_file))
            except ValueError:
                run["file"] = None  # the file lives outside the current workspace
        return run

    def list_runs(
        self,
        *,
        file: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        pipeline_file = str(self.resolve(file)) if file else None
        runs = self.db.list_runs(
            limit=limit, offset=offset, pipeline_file=pipeline_file, status=status
        )
        return [self._public_run(r) for r in runs]

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run is None:
            raise NotFound("run not found")
        return self._public_run(run)

    def get_logs(self, run_id: str, **filters: Any) -> list[dict[str, Any]]:
        if self.db.get_run(run_id) is None:
            raise NotFound("run not found")
        return self.db.get_logs(run_id, **filters)

    def export_logs(self, run_id: str, fmt: str, **filters: Any) -> Iterator[str]:
        """Yield the run's log in ``txt``, ``csv`` or ``jsonl`` format, page by page."""
        if fmt not in ("txt", "csv", "jsonl"):
            raise BadRequest("format must be txt, csv or jsonl")
        if self.db.get_run(run_id) is None:
            raise NotFound("run not found")
        import json

        if fmt == "csv":
            header = io.StringIO()
            csv.writer(header, lineterminator="\n").writerow(
                ["seq", "ts", "step", "stream", "level", "message"]
            )
            yield header.getvalue()
        after = 0
        while True:
            page = self.db.get_logs(run_id, after_seq=after, limit=EXPORT_PAGE, **filters)
            if not page:
                return
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            for row in page:
                if fmt == "txt":
                    level = f" {row['level']}" if row["level"] else ""
                    buffer.write(f"{row['ts']}{level} [{row['step_id']}] {row['message']}\n")
                elif fmt == "csv":
                    writer.writerow(
                        [
                            row["seq"],
                            row["ts"],
                            row["step_id"],
                            row["stream"],
                            row["level"],
                            row["message"],
                        ]
                    )
                else:
                    buffer.write(json.dumps(row, ensure_ascii=False) + "\n")
            yield buffer.getvalue()
            after = page[-1]["seq"]

    def stats(self) -> dict[str, Any]:
        return self.db.stats()

    # -- secrets ---------------------------------------------------------------------------------
    def _secret_names(self) -> list[str]:
        try:
            return self.secrets.names()
        except SecretError:
            return []

    def secrets_overview(self) -> list[dict[str, Any]]:
        """Stored secrets plus any a pipeline refers to but nobody has set yet. Never values."""
        stored = set(self._secret_names())
        used: dict[str, list[str]] = {}
        for path in self._pipeline_files():
            try:
                graph = load_graph(path, self.workspace)
            except ConfigError:
                continue
            names = referenced_secrets([s["params"] for s in graph["steps"]] + graph["notifiers"])
            for name in names:
                used.setdefault(name, []).append(self._rel(path))
        return [
            {"name": name, "exists": name in stored, "used_by": sorted(used.get(name, []))}
            for name in sorted(stored | set(used))
        ]

    def set_secret(self, name: str, value: str) -> None:
        try:
            self.secrets.set(name, value)
        except SecretError as exc:
            raise BadRequest(str(exc)) from exc
        self.bus.publish({"type": "secrets_changed"})

    def delete_secret(self, name: str) -> None:
        try:
            removed = self.secrets.delete(name)
        except SecretError as exc:
            raise BadRequest(str(exc)) from exc
        if not removed:
            raise NotFound("secret not found")
        self.bus.publish({"type": "secrets_changed"})

    # -- misc --------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "settings": self.settings.model_dump(),
            "stats": self.stats(),
            "active": self.active_runs(),
        }


def _looks_like_pipeline(path: Path) -> bool:
    """True for YAML files that declare ``name:`` and ``steps:`` (so broken ones are reported)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines()
    return any(line.startswith("steps:") for line in lines) and any(
        line.startswith("name:") for line in lines
    )
