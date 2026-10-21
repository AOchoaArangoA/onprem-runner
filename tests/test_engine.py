from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from onprem_runner.config import load_config
from onprem_runner.engine import BadRequest, Conflict, Engine, NotFound
from onprem_runner.engine.core import Subscriber
from onprem_runner.models import RunResult, StepResult
from onprem_runner.runner import Runner
from onprem_runner.store import RunStore

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
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    (ws / "scripts" / "fail.py").write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    (ws / "scripts" / "slow.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    (ws / "demo.yaml").write_text(SIMPLE, encoding="utf-8")
    return ws


@pytest.fixture
def engine(tmp_path: Path, workspace: Path) -> Iterator[Engine]:
    eng = Engine(tmp_path / "data", workspace=workspace, cipher=ReverseCipher())
    eng.start()
    yield eng
    eng.close()


def wait_idle(engine: Engine, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while engine.active_runs():
        assert time.monotonic() < deadline, "run did not finish"
        time.sleep(0.05)
    engine.db.flush()


def collect_until_finished(sub: Subscriber, timeout: float = 20.0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = sub.get(timeout=0.5)
        if event is None:
            continue
        events.append(event)
        if event["type"] == "run_finished":
            return events
    raise AssertionError(f"no run_finished; got {[e['type'] for e in events]}")


# -- discovery --------------------------------------------------------------------------


def test_lists_pipelines_and_skips_unrelated_yaml(engine: Engine, workspace: Path) -> None:
    (workspace / "docker-compose.yaml").write_text("services:\n  web: {}\n", encoding="utf-8")
    (workspace / "broken.yaml").write_text(
        "name: Broken\nsteps:\n  - {id: x, type: nope}\n", encoding="utf-8"
    )
    by_file = {p["file"]: p for p in engine.list_pipelines()}
    assert set(by_file) == {"demo.yaml", "broken.yaml"}
    assert by_file["demo.yaml"]["name"] == "Demo"
    assert by_file["demo.yaml"]["steps"] == 2
    assert by_file["demo.yaml"]["last_run"] is None
    assert by_file["broken.yaml"]["error"]


def test_hidden_and_vendor_folders_are_not_scanned(engine: Engine, workspace: Path) -> None:
    for folder in (".opr", ".venv", "node_modules"):
        (workspace / folder).mkdir()
        (workspace / folder / "x.yaml").write_text(SIMPLE, encoding="utf-8")
    assert [p["file"] for p in engine.list_pipelines()] == ["demo.yaml"]


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "../outside.yaml",
        "..\\outside.yaml",
        "scripts/ok.py",
        "missing.yaml",
        "demo.yaml/../../x.yaml",
    ],
)
def test_pipeline_paths_cannot_escape_the_workspace(
    engine: Engine, tmp_path: Path, rel: str
) -> None:
    (tmp_path / "outside.yaml").write_text(SIMPLE, encoding="utf-8")
    with pytest.raises(NotFound):
        engine.resolve(rel)


def test_an_absolute_path_cannot_be_used_either(engine: Engine, tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(SIMPLE, encoding="utf-8")
    with pytest.raises(NotFound):
        engine.resolve(str(outside))


# -- running ----------------------------------------------------------------------------


def test_a_run_streams_events_and_is_stored(engine: Engine) -> None:
    sub = engine.bus.subscribe()
    run_id = engine.start_run("demo.yaml")
    events = collect_until_finished(sub)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert events[-1]["status"] == "success"
    steps = [(e["step_id"], e["status"]) for e in events if e["type"] == "step"]
    assert ("a", "running") in steps and ("a", "success") in steps and ("b", "success") in steps
    logs = [e for e in events if e["type"] == "log"]
    assert {e["message"] for e in logs} == {"ok", "done"}
    assert all(e["run_id"] == run_id for e in events)

    wait_idle(engine)
    run = engine.get_run(run_id)
    assert (run["status"], run["trigger"], run["file"]) == ("success", "manual", "demo.yaml")
    assert [s["step_id"] for s in run["steps"]] == ["a", "b"]
    assert [row["message"] for row in engine.get_logs(run_id)] == ["ok", "done"]
    assert engine.list_pipelines()[0]["last_run"]["run_id"] == run_id


def test_two_runs_of_the_same_pipeline_cannot_overlap(engine: Engine, workspace: Path) -> None:
    (workspace / "slow.yaml").write_text(
        "name: Slow\nsteps:\n  - {id: s, type: python, script: scripts/slow.py}\n", encoding="utf-8"
    )
    run_id = engine.start_run("slow.yaml")
    with pytest.raises(Conflict):
        engine.start_run("slow.yaml")
    engine.start_run("demo.yaml")  # a different pipeline is fine
    engine.cancel_run(run_id)
    wait_idle(engine)


def test_cancel_marks_the_run_cancelled(engine: Engine, workspace: Path) -> None:
    (workspace / "slow.yaml").write_text(
        "name: Slow\nsteps:\n  - {id: s, type: python, script: scripts/slow.py}\n", encoding="utf-8"
    )
    sub = engine.bus.subscribe()
    run_id = engine.start_run("slow.yaml")
    time.sleep(1.0)
    engine.cancel_run(run_id)
    events = collect_until_finished(sub)
    assert events[-1]["status"] == "cancelled"
    wait_idle(engine)
    run = engine.get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["steps"][0]["status"] == "cancelled"
    with pytest.raises(NotFound):
        engine.cancel_run(run_id)  # already finished


def test_partial_run_executes_only_the_selected_steps(engine: Engine) -> None:
    run_id = engine.start_run("demo.yaml", only=["b"])
    wait_idle(engine)
    assert [s["step_id"] for s in engine.get_run(run_id)["steps"]] == ["b"]


def test_unknown_steps_and_missing_pipelines_are_bad_requests(engine: Engine) -> None:
    with pytest.raises(BadRequest, match="nope"):
        engine.start_run("demo.yaml", only=["nope"])
    with pytest.raises(NotFound):
        engine.start_run("ghost.yaml")
    assert engine.active_runs() == []


def test_a_broken_pipeline_cannot_be_started(engine: Engine, workspace: Path) -> None:
    (workspace / "broken.yaml").write_text(
        "name: B\nsteps:\n  - {id: x, type: nope}\n", encoding="utf-8"
    )
    with pytest.raises(BadRequest):
        engine.start_run("broken.yaml")


def test_failed_runs_are_recorded_as_failed(engine: Engine, workspace: Path) -> None:
    (workspace / "bad.yaml").write_text(
        "name: Bad\nsteps:\n  - {id: x, type: python, script: scripts/fail.py}\n", encoding="utf-8"
    )
    run_id = engine.start_run("bad.yaml")
    wait_idle(engine)
    assert engine.get_run(run_id)["status"] == "failed"
    assert [r["run_id"] for r in engine.list_runs(status="failed")] == [run_id]
    assert engine.list_runs(file="bad.yaml")[0]["run_id"] == run_id


def test_secrets_reach_steps_and_are_masked_in_stored_logs(engine: Engine, workspace: Path) -> None:
    engine.set_secret("TOKEN", "tok-abcdef-123")
    (workspace / "scripts" / "leak.py").write_text(
        "import os\nprint('token:', os.environ['T'])\n", encoding="utf-8"
    )
    (workspace / "sec.yaml").write_text(
        "name: Sec\nsteps:\n  - id: s\n    type: python\n    script: scripts/leak.py\n"
        "    env: {T: '${secret:TOKEN}'}\n",
        encoding="utf-8",
    )
    run_id = engine.start_run("sec.yaml")
    wait_idle(engine)
    messages = [row["message"] for row in engine.get_logs(run_id)]
    assert messages == ["token: ***"]
    assert "tok-abcdef-123" not in json.dumps(engine.get_run(run_id))


# -- startup housekeeping ---------------------------------------------------------------


def test_start_fails_orphaned_runs_and_imports_cli_runs(tmp_path: Path, workspace: Path) -> None:
    data = tmp_path / "data2"
    first = Engine(data, workspace=workspace, cipher=ReverseCipher())
    first.db.run_started(
        "20260101-000000-aaaaaa",
        "Demo",
        workspace / "demo.yaml",
        "manual",
        __import__("datetime").datetime.now(),
    )
    first.db.flush()
    first.close()

    # a run made with `opr run` while no engine was open
    config = load_config(workspace / "demo.yaml")
    cli_result = Runner(
        config, workspace, store=RunStore(workspace), pipeline_file=workspace / "demo.yaml"
    ).run()

    second = Engine(data, workspace=workspace, cipher=ReverseCipher())
    try:
        counts = second.start()
        assert counts["interrupted"] == 1
        assert counts["ingested"] >= 1
        assert second.get_run("20260101-000000-aaaaaa")["status"] == "failed"
        imported = second.get_run(cli_result.run_id)
        assert imported["trigger"] == "cli"
        assert imported["file"] == "demo.yaml"
    finally:
        second.close()


# -- editing ----------------------------------------------------------------------------


def test_save_graph_round_trip_and_events(engine: Engine, workspace: Path) -> None:
    sub = engine.bus.subscribe()
    graph = engine.get_graph("demo.yaml")
    graph["steps"].append({"id": "c", "type": "shell", "params": {"command": "echo c"}})
    graph["edges"].append({"id": "x", "source": "b", "target": "c", "when": "always"})
    result = engine.save_graph("demo.yaml", graph, etag=graph["etag"])
    assert result["etag"] != graph["etag"]
    assert sub.get(timeout=2)["type"] == "pipelines_changed"
    assert "echo c" in (workspace / "demo.yaml").read_text(encoding="utf-8")
    assert engine.list_pipelines()[0]["steps"] == 3


def test_save_conflict_and_invalid_graph(engine: Engine, workspace: Path) -> None:
    graph = engine.get_graph("demo.yaml")
    (workspace / "demo.yaml").write_text(SIMPLE + "# changed elsewhere\n", encoding="utf-8")
    with pytest.raises(Conflict):
        engine.save_graph("demo.yaml", graph, etag=graph["etag"])
    fresh = engine.get_graph("demo.yaml")
    fresh["edges"].append({"id": "c", "source": "b", "target": "a", "when": "success"})
    with pytest.raises(BadRequest, match="cycle"):
        engine.save_graph("demo.yaml", fresh, etag=fresh["etag"])
    assert engine.save_graph("demo.yaml", graph, etag=None, force=True)["etag"]


def test_validate_graph_does_not_write(engine: Engine, workspace: Path) -> None:
    before = (workspace / "demo.yaml").read_bytes()
    graph = engine.get_graph("demo.yaml")
    assert engine.validate_graph("demo.yaml", graph) == {"ok": True, "error": None}
    graph["edges"].append({"id": "c", "source": "b", "target": "a", "when": "success"})
    verdict = engine.validate_graph("demo.yaml", graph)
    assert verdict["ok"] is False and "cycle" in verdict["error"]
    assert engine.validate_graph("demo.yaml", {"junk": 1})["ok"] is False
    assert (workspace / "demo.yaml").read_bytes() == before


def test_create_and_delete_pipeline(engine: Engine, workspace: Path) -> None:
    created = engine.create_pipeline("Nuevo flujo")
    assert created == {"file": "nuevo-flujo.yaml"}
    assert "nuevo-flujo.yaml" in [p["file"] for p in engine.list_pipelines()]
    engine.delete_pipeline("nuevo-flujo.yaml")
    assert not (workspace / "nuevo-flujo.yaml").exists()
    [trashed] = list((engine.data_dir / "trash").glob("*nuevo-flujo.yaml"))
    assert "Nuevo flujo" in trashed.read_text(encoding="utf-8")
    with pytest.raises(BadRequest):
        engine.create_pipeline("  ")


def test_a_running_pipeline_cannot_be_deleted(engine: Engine, workspace: Path) -> None:
    (workspace / "slow.yaml").write_text(
        "name: Slow\nsteps:\n  - {id: s, type: python, script: scripts/slow.py}\n", encoding="utf-8"
    )
    run_id = engine.start_run("slow.yaml")
    with pytest.raises(Conflict):
        engine.delete_pipeline("slow.yaml")
    engine.cancel_run(run_id)
    wait_idle(engine)


# -- secrets & settings -----------------------------------------------------------------


def test_secrets_overview_lists_missing_references_without_values(
    engine: Engine, workspace: Path
) -> None:
    (workspace / "uses.yaml").write_text(
        "name: U\nsteps:\n  - {id: s, type: shell, command: 'echo ${secret:API_KEY}'}\n",
        encoding="utf-8",
    )
    overview = {s["name"]: s for s in engine.secrets_overview()}
    assert overview["API_KEY"] == {"name": "API_KEY", "exists": False, "used_by": ["uses.yaml"]}
    assert engine.list_pipelines()[-1]["secrets_missing"] == ["API_KEY"]

    engine.set_secret("API_KEY", "super-secret-value")
    engine.set_secret("UNUSED", "other-secret")
    overview = {s["name"]: s for s in engine.secrets_overview()}
    assert overview["API_KEY"]["exists"] is True
    assert overview["UNUSED"] == {"name": "UNUSED", "exists": True, "used_by": []}
    assert "super-secret-value" not in json.dumps(overview)
    assert engine.list_pipelines()[-1]["secrets_missing"] == []

    engine.delete_secret("UNUSED")
    with pytest.raises(NotFound):
        engine.delete_secret("UNUSED")
    with pytest.raises(BadRequest):
        engine.set_secret("bad name", "x")


def test_settings_validation_and_workspace_change(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(BadRequest):
        engine.update_settings({"retention_days": 0})
    with pytest.raises(BadRequest, match="existing folder"):
        engine.update_settings({"workspace": str(tmp_path / "nope")})
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.yaml").write_text(SIMPLE.replace("Demo", "Other"), encoding="utf-8")
    sub = engine.bus.subscribe()
    engine.update_settings({"workspace": str(other), "language": "es"})
    assert [p["name"] for p in engine.list_pipelines()] == ["Other"]
    types = {sub.get(timeout=1)["type"], sub.get(timeout=1)["type"]}
    assert types == {"settings_changed", "pipelines_changed"}
    assert engine.state()["settings"]["language"] == "es"


# -- exports ----------------------------------------------------------------------------


def test_export_formats(engine: Engine) -> None:
    run_id = engine.start_run("demo.yaml")
    wait_idle(engine)
    txt = "".join(engine.export_logs(run_id, "txt"))
    assert "[a] ok" in txt and "[b] done" in txt
    rows = list(csv.reader(io.StringIO("".join(engine.export_logs(run_id, "csv")))))
    assert rows[0] == ["seq", "ts", "step", "stream", "level", "message"]
    assert [r[5] for r in rows[1:]] == ["ok", "done"]
    jsonl = [json.loads(line) for line in "".join(engine.export_logs(run_id, "jsonl")).splitlines()]
    assert [r["message"] for r in jsonl] == ["ok", "done"]
    with pytest.raises(BadRequest):
        list(engine.export_logs(run_id, "xml"))
    with pytest.raises(NotFound):
        list(engine.export_logs("20260101-000000-ffffff", "txt"))


def test_export_pages_through_large_logs(engine: Engine) -> None:
    from datetime import datetime

    from onprem_runner.logs import LogLine

    run_id = "20260101-000000-bbbbbb"
    engine.db.run_started(run_id, "p", None, "manual", datetime.now())
    for i in range(1, 12_001):
        engine.db.log(LogLine(run_id, "s", i, datetime.now(), "stdout", None, f"l{i}", None))
    engine.db.flush()
    lines = "".join(engine.export_logs(run_id, "txt")).splitlines()
    assert len(lines) == 12_000
    assert lines[-1].endswith("l12000")


# -- events -----------------------------------------------------------------------------


def test_slow_subscribers_are_resynced_instead_of_growing_forever() -> None:
    sub = Subscriber()
    for i in range(10_000):
        sub.offer({"type": "log", "n": i})
    assert sub.get(timeout=0.1) == {"type": "resync"}
    assert sub.get(timeout=0.05) is None  # the backlog was dropped


def test_events_reach_every_subscriber_and_unsubscribe_works(engine: Engine) -> None:
    one, two = engine.bus.subscribe(), engine.bus.subscribe()
    engine.bus.publish({"type": "x"})
    assert one.get(1) == {"type": "x"} and two.get(1) == {"type": "x"}
    engine.bus.unsubscribe(one)
    engine.bus.publish({"type": "y"})
    assert one.get(0.05) is None and two.get(1) == {"type": "y"}


def test_closing_the_engine_cancels_running_pipelines(tmp_path: Path, workspace: Path) -> None:
    (workspace / "slow.yaml").write_text(
        "name: Slow\nsteps:\n  - {id: s, type: python, script: scripts/slow.py}\n", encoding="utf-8"
    )
    eng = Engine(tmp_path / "data3", workspace=workspace, cipher=ReverseCipher())
    eng.start()
    eng.start_run("slow.yaml")
    time.sleep(1.0)
    started = time.monotonic()
    eng.close()
    assert time.monotonic() - started < 15
    reopened = Engine(tmp_path / "data3", workspace=workspace, cipher=ReverseCipher())
    try:
        assert reopened.list_runs()[0]["status"] == "cancelled"
    finally:
        reopened.close()


def test_result_types_are_importable() -> None:
    assert RunResult and StepResult
