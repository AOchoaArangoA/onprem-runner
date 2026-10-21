"""Local HTTP API of the engine (loopback only, token-protected, with server-sent events)."""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from onprem_runner import __version__
from onprem_runner.engine.core import BadRequest, Engine, EngineError, NotFound
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.store import RUN_ID_PATTERN

log = logging.getLogger(LOGGER_NAME)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MAX_BODY = 2_000_000
KEEPALIVE_SECONDS = 15.0
LOCK_NAME = "engine.lock"
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class EngineAlreadyRunning(Exception):
    """Another engine already uses this data directory."""


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        engine: Engine,
        token: str,
        ui_dir: Path | None = None,
    ) -> None:
        super().__init__(address, Handler)
        self.engine = engine
        self.token = token
        self.ui_dir = ui_dir.resolve() if ui_dir else None
        self.restrict_hosts = address[0] in LOOPBACK_HOSTS
        self.stopping = threading.Event()

    def request_shutdown(self) -> None:
        self.stopping.set()
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "opr-engine"

    @property
    def api(self) -> ApiServer:
        return cast(ApiServer, self.server)

    @property
    def engine(self) -> Engine:
        return self.api.engine

    def log_message(self, format: str, *args: Any) -> None:  # silence the default stderr log
        log.debug("api %s", format % args)

    # -- plumbing ----------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        if not self.api.restrict_hosts:
            return True
        host = self.headers.get("Host", "")
        hostname = host if host.endswith("]") else host.rsplit(":", 1)[0]
        return hostname in LOOPBACK_HOSTS

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else query.get("token", [""])[0]
        return hmac.compare_digest(supplied, self.api.token)

    def _origin_ok(self) -> bool:
        """Browsers send Origin on cross-site requests; only our own origin may write."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client (the CLI, tests); the token still applies
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise BadRequest("invalid Content-Length") from exc
        if length > MAX_BODY:
            raise BadRequest("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise BadRequest("body must be JSON") from exc
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    # -- dispatch ----------------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = unquote(url.path)
        try:
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "invalid host")
                return
            if not path.startswith("/api/"):
                if method == "GET":
                    self._static(path)
                else:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            if path == "/api/health":
                self._json(HTTPStatus.OK, {"ok": True, "version": __version__, "pid": os.getpid()})
                return
            if not self._authorized(query):
                self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
                return
            if method != "GET" and not self._origin_ok():
                self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
                return
            handler, args = self._match(method, path)
            if handler is None:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            handler(query, *args)
        except EngineError as exc:
            self._error(exc.status, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away (closed tab, cancelled export)
        except Exception:
            log.exception("unhandled error serving %s %s", method, path)
            with contextlib.suppress(Exception):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _match(self, method: str, path: str) -> tuple[Callable[..., None] | None, tuple[str, ...]]:
        for verb, pattern, handler in self._routes():
            if verb != method:
                continue
            match = re.fullmatch(pattern, path)
            if match:
                return handler, match.groups()
        return None, ()

    def _routes(self) -> list[tuple[str, str, Callable[..., None]]]:
        return [
            ("GET", r"/api/state", self._state),
            ("GET", r"/api/events", self._events),
            ("GET", r"/api/pipelines", self._pipelines),
            ("POST", r"/api/pipelines", self._create_pipeline),
            ("GET", r"/api/pipeline", self._get_pipeline),
            ("PUT", r"/api/pipeline", self._save_pipeline),
            ("DELETE", r"/api/pipeline", self._delete_pipeline),
            ("POST", r"/api/pipeline/validate", self._validate_pipeline),
            ("POST", r"/api/pipeline/run", self._run_pipeline),
            ("POST", r"/api/pipeline/schedule", self._pause_schedule),
            ("GET", r"/api/runs", self._runs),
            ("GET", r"/api/runs/([^/]+)", self._run),
            ("POST", r"/api/runs/([^/]+)/cancel", self._cancel),
            ("GET", r"/api/runs/([^/]+)/logs", self._logs),
            ("GET", r"/api/runs/([^/]+)/export", self._export),
            ("GET", r"/api/secrets", self._secrets),
            ("PUT", r"/api/secrets/([^/]+)", self._set_secret),
            ("DELETE", r"/api/secrets/([^/]+)", self._delete_secret),
            ("GET", r"/api/settings", self._get_settings),
            ("PUT", r"/api/settings", self._put_settings),
            ("GET", r"/api/interpreters", self._interpreters),
            ("POST", r"/api/interpreters/check", self._check_interpreter),
            ("POST", r"/api/shutdown", self._shutdown),
        ]

    # -- helpers for handlers ----------------------------------------------------------
    @staticmethod
    def _one(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
        values = query.get(key)
        return values[0] if values else default

    def _file(self, query: dict[str, list[str]]) -> str:
        file = self._one(query, "file")
        if not file:
            raise BadRequest("missing ?file=")
        return file

    @staticmethod
    def _int(query: dict[str, list[str]], key: str, default: int) -> int:
        value = query.get(key, [str(default)])[0]
        try:
            return int(value)
        except ValueError as exc:
            raise BadRequest(f"{key} must be a number") from exc

    @staticmethod
    def _run_id(value: str) -> str:
        if not RUN_ID_PATTERN.match(value):
            raise NotFound("run not found")
        return value

    def _log_filters(self, query: dict[str, list[str]]) -> dict[str, Any]:
        levels = [lv for raw in query.get("level", []) for lv in raw.split(",") if lv]
        return {
            "step_id": self._one(query, "step"),
            "levels": levels or None,
            "stream": self._one(query, "stream"),
            "search": self._one(query, "q"),
        }

    # -- handlers ----------------------------------------------------------------------
    def _state(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.state())

    def _pipelines(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.list_pipelines())

    def _create_pipeline(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        self._json(HTTPStatus.CREATED, self.engine.create_pipeline(str(body.get("name", ""))))

    def _get_pipeline(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.get_graph(self._file(query)))

    def _save_pipeline(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        graph = body.get("graph")
        if not isinstance(graph, dict):
            raise BadRequest('expected {"graph": {...}}')
        result = self.engine.save_graph(
            self._file(query), graph, etag=body.get("etag"), force=bool(body.get("force"))
        )
        self._json(HTTPStatus.OK, result)

    def _delete_pipeline(self, query: dict[str, list[str]]) -> None:
        self.engine.delete_pipeline(self._file(query))
        self._json(HTTPStatus.OK, {"ok": True})

    def _validate_pipeline(self, query: dict[str, list[str]]) -> None:
        graph = self._body().get("graph")
        if not isinstance(graph, dict):
            raise BadRequest('expected {"graph": {...}}')
        self._json(HTTPStatus.OK, self.engine.validate_graph(self._file(query), graph))

    def _run_pipeline(self, query: dict[str, list[str]]) -> None:
        body = self._body()
        only = body.get("only")
        if only is not None and not (
            isinstance(only, list) and all(isinstance(s, str) for s in only)
        ):
            raise BadRequest('"only" must be a list of step ids')
        run_id = self.engine.start_run(self._file(query), only=only, trigger="manual")
        self._json(HTTPStatus.ACCEPTED, {"run_id": run_id})

    def _pause_schedule(self, query: dict[str, list[str]]) -> None:
        paused = self._body().get("paused")
        if not isinstance(paused, bool):
            raise BadRequest('expected {"paused": true|false}')
        self.engine.set_schedule_paused(self._file(query), paused)
        self._json(HTTPStatus.OK, {"ok": True})

    def _runs(self, query: dict[str, list[str]]) -> None:
        runs = self.engine.list_runs(
            file=self._one(query, "file"),
            status=self._one(query, "status"),
            limit=self._int(query, "limit", 100),
            offset=self._int(query, "offset", 0),
        )
        self._json(HTTPStatus.OK, runs)

    def _run(self, query: dict[str, list[str]], run_id: str) -> None:
        self._json(HTTPStatus.OK, self.engine.get_run(self._run_id(run_id)))

    def _cancel(self, query: dict[str, list[str]], run_id: str) -> None:
        self.engine.cancel_run(self._run_id(run_id))
        self._json(HTTPStatus.ACCEPTED, {"ok": True})

    def _logs(self, query: dict[str, list[str]], run_id: str) -> None:
        rows = self.engine.get_logs(
            self._run_id(run_id),
            after_seq=self._int(query, "after", 0),
            limit=self._int(query, "limit", 1000),
            **self._log_filters(query),
        )
        self._json(HTTPStatus.OK, rows)

    def _export(self, query: dict[str, list[str]], run_id: str) -> None:
        fmt = self._one(query, "format", "txt") or "txt"
        chunks = self.engine.export_logs(self._run_id(run_id), fmt, **self._log_filters(query))
        first = next(chunks, "")  # errors (bad format, unknown run) surface before any header
        mime = {"txt": "text/plain", "csv": "text/csv", "jsonl": "application/x-ndjson"}[fmt]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{run_id}.{fmt}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()  # no Content-Length: the body ends when the connection closes
        self.close_connection = True
        self.wfile.write(first.encode("utf-8"))
        for chunk in chunks:
            self.wfile.write(chunk.encode("utf-8"))

    def _secrets(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.secrets_overview())

    def _set_secret(self, query: dict[str, list[str]], name: str) -> None:
        if not SECRET_NAME.match(name):
            raise BadRequest("invalid secret name")
        value = self._body().get("value")
        if not isinstance(value, str):
            raise BadRequest('expected {"value": "..."}')
        self.engine.set_secret(name, value)
        self._json(HTTPStatus.OK, {"ok": True})  # never echo the value back

    def _delete_secret(self, query: dict[str, list[str]], name: str) -> None:
        self.engine.delete_secret(name)
        self._json(HTTPStatus.OK, {"ok": True})

    def _get_settings(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.settings.model_dump())

    def _put_settings(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.update_settings(self._body()).model_dump())

    def _interpreters(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.OK, self.engine.interpreters())

    def _check_interpreter(self, query: dict[str, list[str]]) -> None:
        path = self._body().get("path")
        if not isinstance(path, str) or not path:
            raise BadRequest('expected {"path": "..."}')
        self._json(HTTPStatus.OK, self.engine.check_interpreter(path))

    def _shutdown(self, query: dict[str, list[str]]) -> None:
        self._json(HTTPStatus.ACCEPTED, {"ok": True})
        self.api.request_shutdown()

    def _events(self, query: dict[str, list[str]]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        subscriber = self.engine.bus.subscribe()
        try:
            self.wfile.write(b'data: {"type": "hello"}\n\n')
            self.wfile.flush()
            while not self.api.stopping.is_set():
                event = subscriber.get(timeout=KEEPALIVE_SECONDS)
                payload = (
                    b": keepalive\n\n"
                    if event is None
                    else b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
                )
                self.wfile.write(payload)
                self.wfile.flush()
        finally:
            self.engine.bus.unsubscribe(subscriber)

    # -- static UI ---------------------------------------------------------------------
    def _static(self, path: str) -> None:
        root = self.api.ui_dir
        if root is None or not root.is_dir():
            self._error(HTTPStatus.NOT_FOUND, "this engine does not serve a UI")
            return
        relative = path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not candidate.is_file():
            if "." in Path(relative).name:  # a missing asset is a 404, not the app shell
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            candidate = root / "index.html"  # single-page app routes
            if not candidate.is_file():
                self._error(HTTPStatus.NOT_FOUND, "ui not built")
                return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)


# -- starting the server --------------------------------------------------------------


def lock_path(data_dir: Path) -> Path:
    return data_dir / LOCK_NAME


def read_lock(data_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(lock_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            return bool(json.loads(response.read()).get("ok"))
    except (OSError, ValueError):
        return False


def serve(
    data_dir: Path,
    *,
    workspace: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    ui_dir: Path | None = None,
    on_ready: Callable[[ApiServer], None] | None = None,
) -> None:
    """Run the engine until it is asked to stop (Ctrl+C, SIGTERM or ``POST /api/shutdown``)."""
    existing = read_lock(data_dir)
    if existing and _healthy(int(existing.get("port", 0))):
        raise EngineAlreadyRunning(f"an engine is already running on port {existing['port']}")

    engine = Engine(data_dir, workspace=workspace)
    engine.start()
    server = ApiServer((host, port), engine, token or secrets.token_urlsafe(32), ui_dir)
    lock = lock_path(data_dir)
    lock.write_text(
        json.dumps({"pid": os.getpid(), "port": server.server_address[1], "token": server.token}),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        os.chmod(lock, 0o600)

    def stop(*_: object) -> None:
        server.request_shutdown()

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, stop)
    try:
        if on_ready is not None:
            on_ready(server)
        server.serve_forever(poll_interval=0.25)
    finally:
        lock.unlink(missing_ok=True)
        server.server_close()
        engine.close()
