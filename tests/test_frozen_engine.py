"""Smoke tests of the packaged engine (desktop/engine-dist). Skipped when it is not built:

python desktop/engine/build_engine.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

EXE = (
    Path(__file__).resolve().parents[1]
    / "desktop"
    / "engine-dist"
    / "opr-engine"
    / "opr-engine.exe"
)

pytestmark = pytest.mark.skipif(
    not EXE.exists() or sys.platform != "win32", reason="packaged engine not built"
)

TOKEN = "frozen-token"


def call(port: int, method: str, path: str, body: Any = None) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode()
        if body is not None
        else (b"{}" if method == "POST" else None),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode()
        return json.loads(text) if text else None


def wait_idle(port: int) -> None:
    deadline = time.monotonic() + 60
    while call(port, "GET", "/api/state")["active"]:
        assert time.monotonic() < deadline
        time.sleep(0.2)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    workspace = tmp_path / "ws"
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts" / "hello.py").write_text(
        "import sys, json\nprint('hello from', sys.version_info[:2])\n"
        "import os\nprint('token:', os.environ.get('T', '-'))\n"
        "print(json.dumps({'level': 'warn', 'msg': 'structured'}))\n",
        encoding="utf-8",
    )
    (workspace / "flow.yaml").write_text(
        "name: Frozen flow\nsteps:\n"
        "  - id: hello\n    type: python\n    script: scripts/hello.py\n"
        "    env: {T: '${secret:TOK}'}\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            str(EXE),
            "serve",
            "--data-dir",
            str(tmp_path / "data"),
            "--workspace",
            str(workspace),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "OPR_TOKEN": TOKEN},
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, proc.stderr.read() if proc.stderr else "engine printed nothing"
    port = json.loads(line)["port"]
    try:
        yield port, workspace
    finally:
        try:
            call(port, "POST", "/api/shutdown")
            proc.wait(timeout=30)
        except Exception:
            proc.kill()


def test_the_packaged_engine_runs_pipelines_with_the_embedded_interpreter(
    engine: tuple[int, Path],
) -> None:
    port, _ = engine
    call(port, "PUT", "/api/secrets/TOK", {"value": "frozen-secret-1"})  # DPAPI from the .exe
    run_id = call(port, "POST", "/api/pipeline/run?file=flow.yaml", {})["run_id"]
    wait_idle(port)
    run = call(port, "GET", f"/api/runs/{run_id}")
    assert run["status"] == "success", run
    logs = call(port, "GET", f"/api/runs/{run_id}/logs")
    messages = [row["message"] for row in logs]
    assert any(m.startswith("hello from") for m in messages)
    assert "token: ***" in messages  # injected, then masked
    assert any(row["level"] == "WARNING" and row["message"] == "structured" for row in logs)


def test_an_external_interpreter_can_be_chosen_in_settings(engine: tuple[int, Path]) -> None:
    port, _ = engine
    call(port, "PUT", "/api/secrets/TOK", {"value": "another-secret-1"})
    call(port, "PUT", "/api/settings", {"default_python": sys.executable})
    run_id = call(port, "POST", "/api/pipeline/run?file=flow.yaml", {})["run_id"]
    wait_idle(port)
    assert call(port, "GET", f"/api/runs/{run_id}")["status"] == "success"
    version = f"({sys.version_info.major}, {sys.version_info.minor})"
    logs = call(port, "GET", f"/api/runs/{run_id}/logs")
    assert any(version in row["message"] for row in logs)


def test_interpreter_detection_works_when_packaged(engine: tuple[int, Path]) -> None:
    port, _ = engine
    interpreters = call(port, "GET", "/api/interpreters")
    assert any(i["version"] for i in interpreters)
    # The engine itself is offered through the "embedded" option, never as a python.exe.
    assert not any(i["path"].lower().endswith("opr-engine.exe") for i in interpreters)


def test_editing_preserves_comments_when_packaged(engine: tuple[int, Path]) -> None:
    port, workspace = engine
    (workspace / "commented.yaml").write_text(
        "# keep me\nname: Commented\nsteps:\n  - {id: a, type: shell, command: echo a}\n",
        encoding="utf-8",
    )
    graph = call(port, "GET", "/api/pipeline?file=commented.yaml")
    graph["steps"][0]["retries"] = 2
    call(port, "PUT", "/api/pipeline?file=commented.yaml", {"graph": graph, "etag": graph["etag"]})
    text = (workspace / "commented.yaml").read_text(encoding="utf-8")
    assert "# keep me" in text and "retries: 2" in text
