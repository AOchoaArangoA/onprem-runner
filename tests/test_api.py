from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from onprem_runner.engine import Engine
from onprem_runner.engine.api import (
    ApiServer,
    EngineAlreadyRunning,
    read_lock,
    serve,
)

TOKEN = "test-token-123"
SIMPLE = (
    "name: Demo\nsteps:\n"
    "  - {id: a, type: python, script: scripts/ok.py}\n"
    "  - {id: b, type: shell, command: echo done, needs: [a]}\n"
)


class ReverseCipher:
    def encrypt(self, data: bytes) -> bytes:
        return b"enc:" + data[::-1]

    def decrypt(self, data: bytes) -> bytes:
        return data[4:][::-1]


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html><body>app shell</body></html>", encoding="utf-8")
    (ui / "assets" / "app.js").write_text("console.log('app')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the ui folder", encoding="utf-8")
    return ui


@pytest.fixture
def server(tmp_path: Path, ui_dir: Path) -> Iterator[ApiServer]:
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "ok.py").write_text(
        'print(\'ok\')\nprint(\'{"level":"warn","msg":"careful"}\')\n', encoding="utf-8"
    )
    (ws / "scripts" / "slow.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    (ws / "demo.yaml").write_text(SIMPLE, encoding="utf-8")
    engine = Engine(tmp_path / "data", workspace=ws, cipher=ReverseCipher())
    engine.start()
    srv = ApiServer(("127.0.0.1", 0), engine, TOKEN, ui_dir)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    srv.request_shutdown()
    time.sleep(0.2)
    srv.server_close()
    engine.close()


def call(
    server: ApiServer,
    method: str,
    path: str,
    body: Any = None,
    *,
    token: str | None = TOKEN,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, Any]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload, status = response.read(), response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        payload, status = exc.read(), exc.code
        content_type = exc.headers.get("Content-Type", "")
    text = payload.decode("utf-8")
    return status, json.loads(text) if content_type.startswith("application/json") else text


def read_sse(server: ApiServer, until: str, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Open /api/events and collect events until one has type ``until``."""
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", f"/api/events?token={TOKEN}")
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream"
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            line = response.fp.readline().decode("utf-8")  # type: ignore[union-attr]
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1]["type"] == until:
                    return events
    finally:
        conn.close()
    raise AssertionError(f"never saw {until}; got {[e['type'] for e in events]}")


def wait_idle(server: ApiServer) -> None:
    deadline = time.monotonic() + 20
    while call(server, "GET", "/api/state")[1]["active"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    server.engine.db.flush()


# -- security -------------------------------------------------------------------------


def test_health_needs_no_token(server: ApiServer) -> None:
    status, body = call(server, "GET", "/api/health", token=None)
    assert status == 200 and body["ok"] is True


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x"])
def test_api_rejects_missing_or_wrong_tokens(server: ApiServer, token: str | None) -> None:
    for method, path in (("GET", "/api/state"), ("POST", "/api/shutdown"), ("GET", "/api/secrets")):
        assert call(server, method, path, token=token)[0] == 401


def test_token_may_be_given_in_the_query_for_eventsource_and_downloads(server: ApiServer) -> None:
    assert call(server, "GET", f"/api/state?token={TOKEN}", token=None)[0] == 200


def test_foreign_host_header_is_refused(server: ApiServer) -> None:
    assert call(server, "GET", "/api/state", headers={"Host": "evil.example.com"})[0] == 403
    assert call(server, "GET", "/", headers={"Host": "evil.example.com"}, token=None)[0] == 403


def test_cross_origin_writes_are_refused_but_same_origin_is_fine(server: ApiServer) -> None:
    port = server.server_address[1]
    body = {"name": "From a web page"}
    status, _ = call(
        server, "POST", "/api/pipelines", body, headers={"Origin": "https://evil.example"}
    )
    assert status == 403
    status, _ = call(
        server, "POST", "/api/pipelines", body, headers={"Origin": f"http://127.0.0.1:{port}"}
    )
    assert status == 201


def test_oversized_and_malformed_bodies_are_rejected(server: ApiServer) -> None:
    assert call(server, "POST", "/api/pipelines", raw=b"{not json")[0] == 400
    assert call(server, "POST", "/api/pipelines", raw=b"[1, 2]")[0] == 400
    with contextlib.suppress(OSError):  # the server may close before 3 MB are sent
        assert call(server, "POST", "/api/pipelines", raw=b"x" * 3_000_000)[0] == 400


def test_unknown_routes_return_json_404(server: ApiServer) -> None:
    status, body = call(server, "GET", "/api/nope")
    assert status == 404 and "error" in body


def test_run_ids_in_urls_are_validated(server: ApiServer) -> None:
    for bad in ("..%2F..%2Fdata", "abc", "20260101-000000-ZZZZZZ"):
        assert call(server, "GET", f"/api/runs/{bad}")[0] == 404
        assert call(server, "GET", f"/api/runs/{bad}/logs")[0] == 404


# -- pipelines ------------------------------------------------------------------------


def test_pipeline_listing_and_graph(server: ApiServer) -> None:
    status, items = call(server, "GET", "/api/pipelines")
    assert status == 200 and [p["file"] for p in items] == ["demo.yaml"]
    status, graph = call(server, "GET", "/api/pipeline?file=demo.yaml")
    assert status == 200
    assert [s["id"] for s in graph["steps"]] == ["a", "b"]
    assert graph["edges"][0]["when"] == "success"
    assert call(server, "GET", "/api/pipeline")[0] == 400  # ?file= is required
    assert call(server, "GET", "/api/pipeline?file=..%2Fx.yaml")[0] == 404


def test_edit_validate_save_and_conflict_over_http(server: ApiServer) -> None:
    _, graph = call(server, "GET", "/api/pipeline?file=demo.yaml")
    graph["steps"].append({"id": "c", "type": "shell", "params": {"command": "echo c"}})
    graph["edges"].append({"id": "x", "source": "b", "target": "c", "when": "failure"})
    assert call(server, "POST", "/api/pipeline/validate?file=demo.yaml", {"graph": graph})[1]["ok"]

    status, saved = call(
        server, "PUT", "/api/pipeline?file=demo.yaml", {"graph": graph, "etag": graph["etag"]}
    )
    assert status == 200 and saved["etag"] != graph["etag"]
    status, body = call(
        server, "PUT", "/api/pipeline?file=demo.yaml", {"graph": graph, "etag": graph["etag"]}
    )
    assert status == 409  # the etag is stale now

    bad = json.loads(json.dumps(graph))
    bad["edges"].append({"id": "y", "source": "c", "target": "a", "when": "success"})
    status, body = call(server, "POST", "/api/pipeline/validate?file=demo.yaml", {"graph": bad})
    assert status == 200 and body["ok"] is False and "cycle" in body["error"]
    assert (
        call(server, "PUT", "/api/pipeline?file=demo.yaml", {"graph": bad, "force": True})[0] == 400
    )
    assert call(server, "PUT", "/api/pipeline?file=demo.yaml", {"nothing": 1})[0] == 400


def test_create_and_delete_pipeline(server: ApiServer) -> None:
    status, body = call(server, "POST", "/api/pipelines", {"name": "Mi flujo"})
    assert status == 201 and body == {"file": "mi-flujo.yaml"}
    assert call(server, "DELETE", "/api/pipeline?file=mi-flujo.yaml")[0] == 200
    assert call(server, "GET", "/api/pipeline?file=mi-flujo.yaml")[0] == 404


# -- running, live events, history ----------------------------------------------------


def test_a_run_is_visible_live_and_afterwards(server: ApiServer) -> None:
    collected: list[dict[str, Any]] = []
    ready = threading.Event()

    def listen() -> None:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        conn.request("GET", f"/api/events?token={TOKEN}")
        response = conn.getresponse()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = response.fp.readline().decode("utf-8")  # type: ignore[union-attr]
            if line.startswith("data: "):
                event = json.loads(line[6:])
                collected.append(event)
                if event["type"] == "hello":
                    ready.set()
                if event["type"] == "run_finished":
                    break
        conn.close()

    thread = threading.Thread(target=listen)
    thread.start()
    assert ready.wait(10)
    status, body = call(server, "POST", "/api/pipeline/run?file=demo.yaml", {})
    assert status == 202
    run_id = body["run_id"]
    thread.join(timeout=25)

    kinds = [e["type"] for e in collected]
    assert kinds[:2] == ["hello", "run_started"] and kinds[-1] == "run_finished"
    assert collected[-1]["status"] == "success"
    assert {e["message"] for e in collected if e["type"] == "log"} == {"ok", "careful", "done"}

    wait_idle(server)
    status, run = call(server, "GET", f"/api/runs/{run_id}")
    assert status == 200 and run["status"] == "success" and run["file"] == "demo.yaml"
    assert [s["step_id"] for s in run["steps"]] == ["a", "b"]
    assert [r["run_id"] for r in call(server, "GET", "/api/runs?file=demo.yaml")[1]] == [run_id]
    assert call(server, "GET", "/api/runs?status=failed")[1] == []
    assert call(server, "GET", "/api/runs?limit=abc")[0] == 400


def test_log_filters_over_http(server: ApiServer) -> None:
    _, started = call(server, "POST", "/api/pipeline/run?file=demo.yaml", {})
    run_id = started["run_id"]
    wait_idle(server)
    logs = f"/api/runs/{run_id}/logs"
    assert [r["message"] for r in call(server, "GET", logs)[1]] == ["ok", "careful", "done"]
    assert [r["message"] for r in call(server, "GET", logs + "?level=warning")[1]] == ["careful"]
    assert [r["message"] for r in call(server, "GET", logs + "?level=NONE")[1]] == ["ok", "done"]
    assert [r["message"] for r in call(server, "GET", logs + "?step=b")[1]] == ["done"]
    assert [r["message"] for r in call(server, "GET", logs + "?q=CARE")[1]] == ["careful"]
    first = call(server, "GET", logs + "?limit=1")[1]
    assert [r["message"] for r in call(server, "GET", logs + f"?after={first[0]['seq']}")[1]] == [
        "careful",
        "done",
    ]


def test_exports_have_the_right_content_type_and_content(server: ApiServer) -> None:
    _, started = call(server, "POST", "/api/pipeline/run?file=demo.yaml", {})
    run_id = started["run_id"]
    wait_idle(server)
    base = f"/api/runs/{run_id}/export"
    status, txt = call(server, "GET", base + "?format=txt")
    assert status == 200 and "[a] ok" in txt
    status, csv_text = call(server, "GET", base + "?format=csv")
    assert csv_text.splitlines()[0] == "seq,ts,step,stream,level,message"
    status, jsonl = call(server, "GET", base + "?format=jsonl&level=WARNING")
    assert [json.loads(line)["message"] for line in jsonl.splitlines()] == ["careful"]
    assert call(server, "GET", base + "?format=xml")[0] == 400

    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{base}?format=csv&token={TOKEN}"
    )
    with urllib.request.urlopen(request) as response:
        assert response.headers["Content-Type"].startswith("text/csv")
        assert f"{run_id}.csv" in response.headers["Content-Disposition"]


def test_cancel_over_http(server: ApiServer, tmp_path: Path) -> None:
    ws = Path(server.engine.workspace)
    (ws / "slow.yaml").write_text(
        "name: Slow\nsteps:\n  - {id: s, type: python, script: scripts/slow.py}\n", encoding="utf-8"
    )
    _, started = call(server, "POST", "/api/pipeline/run?file=slow.yaml", {})
    run_id = started["run_id"]
    time.sleep(1.0)
    assert call(server, "POST", "/api/pipeline/run?file=slow.yaml", {})[0] == 409
    assert call(server, "POST", f"/api/runs/{run_id}/cancel")[0] == 202
    wait_idle(server)
    assert call(server, "GET", f"/api/runs/{run_id}")[1]["status"] == "cancelled"
    assert call(server, "POST", f"/api/runs/{run_id}/cancel")[0] == 404


def test_partial_run_and_bad_only(server: ApiServer) -> None:
    _, started = call(server, "POST", "/api/pipeline/run?file=demo.yaml", {"only": ["b"]})
    wait_idle(server)
    run = call(server, "GET", f"/api/runs/{started['run_id']}")[1]
    assert [s["step_id"] for s in run["steps"]] == ["b"]
    assert call(server, "POST", "/api/pipeline/run?file=demo.yaml", {"only": "b"})[0] == 400
    assert call(server, "POST", "/api/pipeline/run?file=demo.yaml", {"only": ["zzz"]})[0] == 400


# -- secrets, settings, interpreters --------------------------------------------------


def test_secrets_never_come_back(server: ApiServer) -> None:
    status, body = call(server, "PUT", "/api/secrets/DB_PASSWORD", {"value": "hunter2-hunter2"})
    assert status == 200 and "hunter2" not in json.dumps(body)
    status, listing = call(server, "GET", "/api/secrets")
    assert listing == [{"name": "DB_PASSWORD", "exists": True, "used_by": []}]
    assert "hunter2" not in json.dumps(listing)
    assert call(server, "PUT", "/api/secrets/bad-name", {"value": "x"})[0] == 400
    assert call(server, "PUT", "/api/secrets/OK_NAME", {"value": 5})[0] == 400
    assert call(server, "PUT", "/api/secrets/OK_NAME", {"value": ""})[0] == 400
    assert call(server, "DELETE", "/api/secrets/DB_PASSWORD")[0] == 200
    assert call(server, "DELETE", "/api/secrets/DB_PASSWORD")[0] == 404


def test_settings_round_trip_and_validation(server: ApiServer) -> None:
    status, settings = call(
        server, "PUT", "/api/settings", {"language": "en", "retention_days": 30}
    )
    assert status == 200 and settings["language"] == "en"
    assert call(server, "GET", "/api/settings")[1]["retention_days"] == 30
    assert call(server, "PUT", "/api/settings", {"language": "klingon"})[0] == 400
    assert call(server, "PUT", "/api/settings", {"workspace": "Z:/does/not/exist"})[0] == 400


def test_interpreters_listing_and_check(server: ApiServer) -> None:
    import sys

    status, listing = call(server, "GET", "/api/interpreters")
    assert status == 200 and isinstance(listing, list)
    status, ok = call(server, "POST", "/api/interpreters/check", {"path": sys.executable})
    assert ok["ok"] is True and ok["version"]
    assert (
        call(server, "POST", "/api/interpreters/check", {"path": "C:/nope/python.exe"})[1]["ok"]
        is False
    )
    assert call(server, "POST", "/api/interpreters/check", {})[0] == 400


# -- static UI ------------------------------------------------------------------------


def test_ui_is_served_without_a_token(server: ApiServer) -> None:
    status, html = call(server, "GET", "/", token=None)
    assert status == 200 and "app shell" in html
    status, js = call(server, "GET", "/assets/app.js", token=None)
    assert status == 200 and "console.log" in js
    status, spa = call(server, "GET", "/pipelines/demo", token=None)  # client-side route
    assert status == 200 and "app shell" in spa


def test_ui_does_not_leak_files_outside_its_folder(server: ApiServer) -> None:
    assert call(server, "GET", "/assets/missing.js", token=None)[0] == 404
    for attempt in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        _, body = call(server, "GET", attempt, token=None)
        assert "outside the ui folder" not in str(body)
    assert call(server, "POST", "/", {}, token=None)[0] == 404


# -- starting and stopping ------------------------------------------------------------


def test_shutdown_endpoint_stops_the_server(server: ApiServer) -> None:
    assert call(server, "POST", "/api/shutdown")[0] == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            call(server, "GET", "/api/health", token=None)
        except OSError:
            return
        time.sleep(0.1)
    raise AssertionError("server still answering after shutdown")


def test_serve_writes_a_lock_file_and_refuses_a_second_engine(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ws = tmp_path / "ws"
    ws.mkdir()
    ready: list[ApiServer] = []
    thread = threading.Thread(
        target=serve,
        args=(data,),
        kwargs={"workspace": ws, "token": "tok", "on_ready": ready.append},
    )
    thread.start()
    deadline = time.monotonic() + 20
    while not ready and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready, "engine did not start"
    try:
        lock = read_lock(data)
        assert lock is not None
        assert lock["port"] == ready[0].server_address[1] and lock["token"] == "tok"
        with pytest.raises(EngineAlreadyRunning):
            serve(data, workspace=ws)
    finally:
        ready[0].request_shutdown()
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert read_lock(data) is None  # the lock is removed on a clean stop


def test_a_stale_lock_does_not_block_startup(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "engine.lock").write_text(
        json.dumps({"pid": 1, "port": 1, "token": "x"}), encoding="utf-8"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    ready: list[ApiServer] = []
    thread = threading.Thread(
        target=serve, args=(data,), kwargs={"workspace": ws, "on_ready": ready.append}
    )
    thread.start()
    deadline = time.monotonic() + 20
    while not ready and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready
    ready[0].request_shutdown()
    thread.join(timeout=20)
