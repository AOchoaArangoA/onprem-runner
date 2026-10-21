from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import write_pipeline

pytest.importorskip("duckdb")

from onprem_runner.config import load_config
from onprem_runner.db import Database
from onprem_runner.logs import LogLine
from onprem_runner.models import RunResult, StepResult
from onprem_runner.runner import Runner
from onprem_runner.store import RunStore


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "data" / "opr.duckdb", flush_interval=0.05)
    yield database
    database.close()


def run_with_db(project: Path, db: Database, body: str, trigger: str = "manual") -> RunResult:
    runner = Runner(
        load_config(write_pipeline(project, body)),
        project,
        pipeline_file=project / "pipeline.yaml",
        recorder=db,
    )
    result = runner.run(trigger=trigger)
    db.flush()
    return result


def make_line(run_id: str, seq: int, message: str = "m", level: str | None = None) -> LogLine:
    return LogLine(run_id, "s", seq, datetime.now(), "stdout", level, message, None)


# -- schema ------------------------------------------------------------------------


def test_schema_is_created_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "opr.duckdb"
    first = Database(path)
    assert first.schema_version == 1
    now = datetime.now()
    first.run_started("20260101-000000-aaaaaa", "p", None, "manual", now)
    first.close()

    second = Database(path)  # must not re-run migration 1 over existing tables
    assert second.schema_version == 1
    assert second.get_run("20260101-000000-aaaaaa") is not None
    second.close()


# -- recording a real run -------------------------------------------------------------


def test_a_run_is_recorded_with_steps_and_logs(project: Path, db: Database) -> None:
    (project / "scripts" / "talk.py").write_text(
        'import sys\nprint(\'hello\')\nprint(\'{"level":"warn","msg":"careful","n":3}\')\n'
        "print('oops', file=sys.stderr)\n",
        encoding="utf-8",
    )
    result = run_with_db(
        project,
        db,
        "name: demo\nsteps:\n"
        "  - {id: talk, type: python, script: scripts/talk.py}\n"
        "  - {id: after, type: shell, command: echo done, needs: [talk]}\n",
    )
    run = db.get_run(result.run_id)
    assert run is not None
    assert run["status"] == "success"
    assert run["trigger"] == "manual"
    assert run["pipeline_name"] == "demo"
    assert run["finished_at"] is not None
    assert [s["step_id"] for s in run["steps"]] == ["talk", "after"]
    assert all(s["status"] == "success" for s in run["steps"])
    assert run["steps"][0]["started_at"] is not None

    lines = db.get_logs(result.run_id)
    messages = [(line["step_id"], line["message"]) for line in lines]
    assert ("talk", "hello") in messages
    assert ("after", "done") in messages
    careful = next(line for line in lines if line["message"] == "careful")
    assert careful["level"] == "WARNING"
    assert careful["fields"] == {"n": 3}
    assert [line["seq"] for line in lines] == sorted(line["seq"] for line in lines)


def test_failed_and_skipped_steps_are_recorded(project: Path, db: Database) -> None:
    result = run_with_db(
        project,
        db,
        "name: demo\nsteps:\n"
        "  - {id: bad, type: python, script: scripts/fail.py}\n"
        "  - {id: next, type: python, script: scripts/ok.py, needs: [bad]}\n",
    )
    run = db.get_run(result.run_id)
    assert run is not None
    assert run["status"] == "failed"
    by_id = {s["step_id"]: s for s in run["steps"]}
    assert by_id["bad"]["status"] == "failed"
    assert by_id["bad"]["exit_code"] == 3
    assert by_id["next"]["status"] == "skipped"
    assert db.list_runs(status="failed")[0]["run_id"] == result.run_id
    assert db.list_runs(status="success") == []


def test_list_runs_orders_newest_first_and_filters_by_pipeline(project: Path, db: Database) -> None:
    body = "name: demo\nsteps:\n  - {id: a, type: python, script: scripts/ok.py}\n"
    first = run_with_db(project, db, body)
    time.sleep(1.1)  # run ids and start times have one-second resolution
    second = run_with_db(project, db, body)
    ids = [r["run_id"] for r in db.list_runs()]
    assert ids == [second.run_id, first.run_id]
    assert len(db.list_runs(pipeline_file=str(project / "pipeline.yaml"))) == 2
    assert db.list_runs(pipeline_file="nope.yaml") == []
    assert len(db.list_runs(limit=1)) == 1


# -- log queries --------------------------------------------------------------------


def seeded(db: Database) -> str:
    run_id = "20260101-000000-bbbbbb"
    db.run_started(run_id, "p", None, "manual", datetime.now())
    rows = [
        ("start", None),
        ("disk 100% full", "ERROR"),
        ("a_b literal", "INFO"),
        ("slow query", "WARNING"),
        ("done", None),
    ]
    for i, (message, level) in enumerate(rows, start=1):
        db.log(make_line(run_id, i, message, level))
    db.flush()
    return run_id


def test_filter_by_level_including_none(db: Database) -> None:
    run_id = seeded(db)
    assert [r["message"] for r in db.get_logs(run_id, levels=["error"])] == ["disk 100% full"]
    both = db.get_logs(run_id, levels=["WARNING", "NONE"])
    assert [r["message"] for r in both] == ["start", "slow query", "done"]


def test_search_treats_wildcards_literally(db: Database) -> None:
    run_id = seeded(db)
    assert [r["message"] for r in db.get_logs(run_id, search="100%")] == ["disk 100% full"]
    assert [r["message"] for r in db.get_logs(run_id, search="a_b")] == ["a_b literal"]
    assert db.get_logs(run_id, search="a_x") == []  # '_' must not match any character
    assert len(db.get_logs(run_id, search="SLOW")) == 1  # case-insensitive


def test_after_seq_and_limit_page_through_lines(db: Database) -> None:
    run_id = seeded(db)
    first = db.get_logs(run_id, limit=2)
    assert [r["seq"] for r in first] == [1, 2]
    rest = db.get_logs(run_id, after_seq=first[-1]["seq"])
    assert [r["seq"] for r in rest] == [3, 4, 5]


# -- volume -------------------------------------------------------------------------


def test_batched_writes_handle_tens_of_thousands_of_lines(db: Database) -> None:
    run_id = "20260101-000000-cccccc"
    db.run_started(run_id, "p", None, "manual", datetime.now())
    total = 30_000
    started = time.monotonic()
    for i in range(1, total + 1):
        db.log(make_line(run_id, i, f"line {i}"))
    db.flush()
    elapsed = time.monotonic() - started
    assert db.stats()["log_lines"] == total
    assert elapsed < 20, f"inserting {total} lines took {elapsed:.1f}s"
    assert db.get_logs(run_id, after_seq=total - 1)[0]["message"] == f"line {total}"


def test_line_limit_per_run_adds_a_marker(tmp_path: Path) -> None:
    database = Database(tmp_path / "x.duckdb", flush_interval=0.05, max_lines_per_run=10)
    try:
        run_id = "20260101-000000-dddddd"
        database.run_started(run_id, "p", None, "manual", datetime.now())
        for i in range(1, 51):
            database.log(make_line(run_id, i, f"line {i}"))
        database.flush()
        lines = database.get_logs(run_id, limit=100)
        assert len(lines) == 11  # 10 stored + 1 warning
        assert lines[-1]["level"] == "WARNING"
        assert "limit" in lines[-1]["message"]
    finally:
        database.close()


def test_reads_while_writing_do_not_fail(db: Database) -> None:
    run_id = "20260101-000000-eeeeee"
    db.run_started(run_id, "p", None, "manual", datetime.now())
    for i in range(1, 2001):
        db.log(make_line(run_id, i))
        if i % 200 == 0:
            db.get_logs(run_id)
            db.list_runs()
            db.stats()
    db.flush()
    assert len(db.get_logs(run_id, limit=5000)) == 2000


# -- maintenance --------------------------------------------------------------------


def test_purge_removes_only_old_finished_runs(db: Database) -> None:
    now = datetime.now()
    old_id, new_id, live_id = (
        "20250101-000000-aaaaaa",
        "20260101-000000-bbbbbb",
        "20250101-000000-cccccc",
    )
    for run_id, started, finish in (
        (old_id, now - timedelta(days=200), True),
        (new_id, now - timedelta(days=1), True),
        (live_id, now - timedelta(days=200), False),  # still running: never purged
    ):
        db.run_started(run_id, "p", None, "manual", started)
        db.step_finished(run_id, StepResult("s", "shell", "success", 0, started_at=started))
        db.log(make_line(run_id, 1))
        if finish:
            db.run_finished(RunResult(run_id, "p", started, started + timedelta(seconds=1)))
    db.flush()

    counts = db.purge(retention_days=90)
    assert counts == {"log_lines": 1, "step_runs": 1, "runs": 1}
    assert db.get_run(old_id) is None
    assert db.get_run(new_id) is not None
    assert db.get_run(live_id) is not None
    assert db.get_logs(old_id) == []


def test_mark_interrupted_fails_orphaned_runs(db: Database) -> None:
    run_id = "20260101-000000-ffffff"
    db.run_started(run_id, "p", None, "manual", datetime.now())
    db.step_started(run_id, "s", "shell", datetime.now())
    db.flush()
    assert db.mark_interrupted() == 1
    run = db.get_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["steps"][0]["status"] == "failed"


# -- spool ingestion ------------------------------------------------------------------


def test_runs_made_without_the_engine_are_ingested_once(project: Path, db: Database) -> None:
    store = RunStore(project)
    (project / "scripts" / "talk.py").write_text(
        'print(\'plain\')\nprint(\'{"level":"info","msg":"hi"}\')\n', encoding="utf-8"
    )
    config = load_config(
        write_pipeline(
            project, "name: demo\nsteps:\n  - {id: talk, type: python, script: scripts/talk.py}\n"
        )
    )
    result = Runner(config, project, store=store, pipeline_file=project / "pipeline.yaml").run()

    assert db.ingest_spool(project) == 1
    db.flush()
    assert db.ingest_spool(project) == 0  # idempotent
    run = db.get_run(result.run_id)
    assert run is not None
    assert run["trigger"] == "cli"
    assert run["status"] == "success"
    messages = {line["message"]: line for line in db.get_logs(result.run_id)}
    assert set(messages) == {"plain", "hi"}
    assert messages["hi"]["level"] == "INFO"


def test_unreadable_spool_files_are_skipped(project: Path, db: Database) -> None:
    runs_dir = project / ".opr" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "20260101-000000-zzzzzz.json").write_text("{not json", encoding="utf-8")
    (runs_dir / "20260101-000001-zzzzzz.json").write_text('{"no": "run_id"}', encoding="utf-8")
    assert db.ingest_spool(project) == 0


def test_hostile_log_text_is_stored_verbatim_and_cannot_inject_sql(db: Database) -> None:
    run_id = "20260101-000000-999999"
    db.run_started(run_id, "p", None, "manual", datetime.now())
    nasty = [
        "it's a trap",
        "'); DROP TABLE log_lines; --",
        "tab\tand\nnewline",
        "emoji \U0001f600 ñ ü 日本語",
        "backslash " + chr(92) + " and " + chr(92) + "' quote",
        "nul" + chr(0) + "byte",
    ]
    for i, text in enumerate(nasty, start=1):
        db.log(make_line(run_id, i, text))
    db.flush()
    stored = [r["message"] for r in db.get_logs(run_id)]
    assert stored[:5] == nasty[:5]
    assert stored[5] == "nulbyte"  # NUL cannot be stored in VARCHAR, so it is dropped
    assert db.stats()["log_lines"] == len(nasty)  # the table is still there
