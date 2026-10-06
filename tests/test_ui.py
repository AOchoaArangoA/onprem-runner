from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import write_pipeline
from onprem_runner.store import RunStore
from onprem_runner.ui import UIServer, create_server

PIPELINE = "name: demo\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"


@pytest.fixture
def server(project: Path) -> Iterator[UIServer]:
    write_pipeline(project, PIPELINE)
    srv = create_server(project, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(
    server: UIServer,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw.startswith(("{", "[")) else raw
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_state_lists_pipelines(server: UIServer) -> None:
    status, state = call(server, "/api/state")
    assert status == 200
    assert [p["name"] for p in state["pipelines"]] == ["demo"]
    assert state["pipelines"][0]["steps"][0]["id"] == "a"
    assert state["runs"] == []


def test_page_embeds_token_and_escapes_nothing_unsafe(server: UIServer) -> None:
    status, html = call(server, "/")
    assert status == 200
    assert server.state.token in html
    assert "__TOKEN__" not in html


def test_run_requires_token(server: UIServer) -> None:
    status, _ = call(
        server,
        "/api/run",
        method="POST",
        body={"file": "pipeline.yaml"},
        headers={"X-OPR-Token": "bad"},
    )
    assert status == 403
    status, _ = call(server, "/api/run", method="POST", body={"file": "pipeline.yaml"})
    assert status == 403


def test_foreign_host_header_is_rejected(server: UIServer) -> None:
    status, _ = call(server, "/api/state", headers={"Host": "evil.example.com"})
    assert status == 403


def test_run_from_ui_records_history(server: UIServer) -> None:
    headers = {"X-OPR-Token": server.state.token}
    status, data = call(
        server, "/api/run", method="POST", body={"file": "pipeline.yaml"}, headers=headers
    )
    assert status == 202
    run_id = data["run_id"]

    for _ in range(100):
        _, state = call(server, "/api/state")
        if state["runs"] and not state["active"]:
            break
        time.sleep(0.1)
    assert state["runs"][0]["run_id"] == run_id
    assert state["runs"][0]["status"] == "success"
    assert state["runs"][0]["file"] == "pipeline.yaml"

    status, detail = call(server, f"/api/run?file=pipeline.yaml&id={run_id}")
    assert status == 200
    assert detail["steps"][0]["stdout"].strip() == "ok"


def test_unknown_pipeline_and_bad_body(server: UIServer) -> None:
    headers = {"X-OPR-Token": server.state.token}
    status, _ = call(
        server, "/api/run", method="POST", body={"file": "../secret.yaml"}, headers=headers
    )
    assert status == 400
    status, _ = call(server, "/api/run", method="POST", body={}, headers=headers)
    assert status == 400


def test_run_detail_rejects_path_traversal(server: UIServer) -> None:
    status, _ = call(server, "/api/run?file=pipeline.yaml&id=../../pipeline")
    assert status == 404


def test_invalid_pipeline_is_reported_not_hidden(project: Path) -> None:
    write_pipeline(project, "name: broken\nsteps:\n  - {id: a, type: nope}\n")
    srv = create_server(project, "127.0.0.1", 0)
    try:
        state = srv.state.snapshot()
    finally:
        srv.server_close()
    assert state["pipelines"][0]["error"]


def test_store_prunes_old_runs(project: Path) -> None:
    from datetime import datetime

    from onprem_runner.models import RunResult

    store = RunStore(project, keep=2)
    now = datetime.now()
    for i in range(4):
        store.save(RunResult(f"20260101-00000{i}-abcdef", "p", now, now))
    assert [r["run_id"] for r in store.recent()] == [
        "20260101-000003-abcdef",
        "20260101-000002-abcdef",
    ]
