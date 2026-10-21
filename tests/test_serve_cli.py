from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from onprem_runner.paths import default_data_dir, is_writable


def start_engine(tmp_path: Path, **extra_env: str) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "OPR_TOKEN"}
    env.update(extra_env)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "onprem_runner",
            "serve",
            "--data-dir",
            str(tmp_path / "data"),
            "--workspace",
            str(ws),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, f"engine printed nothing; stderr: {proc.stderr.read() if proc.stderr else ''}"
    return proc, json.loads(line)


def api(port: int, method: str, path: str, token: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if method == "POST":
        request.data = b"{}"
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def test_serve_announces_readiness_and_stops_on_request(tmp_path: Path) -> None:
    proc, ready = start_engine(tmp_path, OPR_TOKEN="from-electron")
    try:
        assert ready["event"] == "ready"
        assert "token" not in ready  # the caller chose it, so it is not echoed back
        assert isinstance(ready["pid"], int)  # not proc.pid: a venv python.exe is a launcher
        port = ready["port"]
        assert api(port, "GET", "/api/health")[1]["ok"] is True
        status, state = api(port, "GET", "/api/state", token="from-electron")
        assert status == 200 and state["workspace"] == str((tmp_path / "ws").resolve())
        assert (tmp_path / "data" / "engine.lock").exists()
        assert api(port, "POST", "/api/shutdown", token="from-electron")[0] == 202
        assert proc.wait(timeout=30) == 0
        assert not (tmp_path / "data" / "engine.lock").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_generated_token_is_printed_and_works(tmp_path: Path) -> None:
    proc, ready = start_engine(tmp_path)
    try:
        assert len(ready["token"]) >= 32
        assert api(ready["port"], "GET", "/api/state", token=ready["token"])[0] == 200
        api(ready["port"], "POST", "/api/shutdown", token=ready["token"])
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_second_engine_on_the_same_data_folder_is_refused(tmp_path: Path) -> None:
    proc, ready = start_engine(tmp_path, OPR_TOKEN="t")
    try:
        second = subprocess.run(
            [sys.executable, "-m", "onprem_runner", "serve", "--data-dir", str(tmp_path / "data")],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert second.returncode == 2
        assert "already running" in second.stderr
        api(ready["port"], "POST", "/api/shutdown", token="t")
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_killed_engine_leaves_data_that_the_next_start_recovers(tmp_path: Path) -> None:
    """Electron may kill the engine; the next start must still work with the same data."""
    proc, _ = start_engine(tmp_path, OPR_TOKEN="t")
    proc.kill()
    proc.wait()
    again, ready2 = start_engine(tmp_path, OPR_TOKEN="t")  # stale lock, DuckDB replays its WAL
    try:
        assert api(ready2["port"], "GET", "/api/state", token="t")[0] == 200
        api(ready2["port"], "POST", "/api/shutdown", token="t")
        assert again.wait(timeout=30) == 0
    finally:
        if again.poll() is None:
            again.kill()
            again.wait()


# -- paths ----------------------------------------------------------------------------


def test_data_dir_honours_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_DATA_DIR", str(tmp_path / "custom"))
    assert default_data_dir() == tmp_path / "custom"


def test_is_writable(tmp_path: Path) -> None:
    assert is_writable(tmp_path / "new" / "folder")
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")
    assert not is_writable(blocker / "sub")  # a file is in the way
