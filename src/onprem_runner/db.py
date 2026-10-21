"""DuckDB storage for runs, steps and log lines.

DuckDB lets only one process write to a database file, so a single :class:`Database` (owned
by the engine) opens it. Writes go through a queue to one writer thread that inserts log lines
in batches, because row-by-row inserts are slow in DuckDB. Reads use their own cursors.

Requires the optional dependency ``duckdb`` (``pip install "onprem-runner[studio]"``).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.logs import LogLine, parse_line
from onprem_runner.models import RunResult, StepResult

log = logging.getLogger(LOGGER_NAME)

DEFAULT_MAX_LINES_PER_RUN = 200_000
DEFAULT_RETENTION_DAYS = 90

# Numbered migrations; append new ones, never edit an applied one.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """CREATE TABLE runs (
                run_id VARCHAR PRIMARY KEY,
                pipeline_file VARCHAR,
                pipeline_name VARCHAR,
                status VARCHAR,
                trigger VARCHAR,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                duration_s DOUBLE
            )""",
            """CREATE TABLE step_runs (
                run_id VARCHAR,
                step_id VARCHAR,
                type VARCHAR,
                status VARCHAR,
                exit_code INTEGER,
                attempts INTEGER,
                ignored BOOLEAN,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                duration_s DOUBLE,
                PRIMARY KEY (run_id, step_id)
            )""",
            """CREATE TABLE log_lines (
                run_id VARCHAR,
                step_id VARCHAR,
                seq BIGINT,
                ts TIMESTAMP,
                stream VARCHAR,
                level VARCHAR,
                message VARCHAR,
                fields JSON
            )""",
        ],
    ),
]

_LOG_COLUMNS = "run_id, step_id, seq, ts, stream, level, message, fields"


class _Flush:
    def __init__(self) -> None:
        self.done = threading.Event()


_STOP = object()


def _lit(value: Any) -> str:
    """SQL literal for an int, datetime, None or string (quotes doubled, NUL removed)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    return "'" + str(value).replace(chr(0), "").replace("'", "''") + "'"


def _log_row_sql(line: LogLine) -> str:
    fields = json.dumps(line.fields, default=str) if line.fields else None
    values = (
        line.run_id,
        line.step_id,
        line.seq,
        line.ts,
        line.stream,
        line.level,
        line.message,
        fields,
    )
    return "(" + ",".join(_lit(v) for v in values) + ")"


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class Database:
    """Run history and log storage. Also implements the runner's ``RunRecorder`` protocol."""

    def __init__(
        self,
        path: Path,
        *,
        flush_interval: float = 0.25,
        batch_size: int = 500,
        max_lines_per_run: int = DEFAULT_MAX_LINES_PER_RUN,
        queue_size: int = 100_000,
    ) -> None:
        self.path = path
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.max_lines_per_run = max_lines_per_run
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        self._migrate()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._line_counts: dict[str, int] = {}
        self._closed = False
        self._writer = threading.Thread(target=self._write_loop, name="db-writer", daemon=True)
        self._writer.start()

    # -- schema -------------------------------------------------------------------
    def _migrate(self) -> None:
        self._con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
        row = self._con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = row[0] if row and row[0] is not None else 0
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            for statement in statements:
                self._con.execute(statement)
            self._con.execute("INSERT INTO schema_version VALUES (?)", [version])

    @property
    def schema_version(self) -> int:
        row = self._con.cursor().execute("SELECT max(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # -- RunRecorder (called from step threads; only enqueue) -------------------------
    def run_started(
        self,
        run_id: str,
        pipeline_name: str,
        pipeline_file: Path | None,
        trigger: str,
        started_at: datetime,
    ) -> None:
        self._put(
            ("run_started", (run_id, str(pipeline_file or ""), pipeline_name, trigger, started_at))
        )

    def step_started(self, run_id: str, step_id: str, step_type: str, started_at: datetime) -> None:
        self._put(
            (
                "step",
                (run_id, step_id, step_type, "running", None, 0, False, started_at, None, None),
            )
        )

    def step_finished(self, run_id: str, result: StepResult) -> None:
        self._put(
            (
                "step",
                (
                    run_id,
                    result.id,
                    result.type,
                    result.status,
                    result.exit_code,
                    result.attempts,
                    result.ignored,
                    result.started_at,
                    result.finished_at,
                    round(result.duration, 3),
                ),
            )
        )

    def log(self, line: LogLine) -> None:
        self._put(("log", line))

    def run_finished(self, result: RunResult) -> None:
        self._put(("run_finished", result))

    def _put(self, item: Any) -> None:
        if self._closed:
            return
        self._queue.put(item)  # blocks when full: slows chatty children instead of losing lines

    # -- writer thread -----------------------------------------------------------------
    def _write_loop(self) -> None:
        cursor = self._con.cursor()
        pending: list[LogLine] = []
        last_flush = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=self.flush_interval)
            except queue.Empty:
                item = None
            try:
                if item is _STOP:
                    self._flush_logs(cursor, pending)
                    return
                if isinstance(item, _Flush):
                    self._flush_logs(cursor, pending)
                    item.done.set()
                elif item is not None:
                    kind, payload = item
                    if kind == "log":
                        self._accept_line(payload, pending)
                    else:
                        self._apply(cursor, kind, payload)
                due = time.monotonic() - last_flush >= self.flush_interval
                if pending and (len(pending) >= self.batch_size or due):
                    self._flush_logs(cursor, pending)
                    last_flush = time.monotonic()
            except Exception:
                log.exception("database writer failed on an item; continuing")
                pending.clear()

    def _accept_line(self, line: LogLine, pending: list[LogLine]) -> None:
        count = self._line_counts.get(line.run_id, 0) + 1
        self._line_counts[line.run_id] = count
        if count <= self.max_lines_per_run:
            pending.append(line)
        elif count == self.max_lines_per_run + 1:
            pending.append(
                LogLine(
                    line.run_id,
                    line.step_id,
                    line.seq,
                    line.ts,
                    "stderr",
                    "WARNING",
                    f"log limit of {self.max_lines_per_run} lines reached; "
                    "further lines of this run are not stored",
                    None,
                )
            )

    def _flush_logs(self, cursor: Any, pending: list[LogLine]) -> None:
        # Values are inlined as escaped literals on purpose: DuckDB's Python binding spends
        # ~1.5 ms per bound parameter (it probes for optional modules), which made a
        # 500-row batch take 6 s instead of 30 ms.
        for start in range(0, len(pending), self.batch_size):
            rows = ",".join(_log_row_sql(line) for line in pending[start : start + self.batch_size])
            cursor.execute(f"INSERT INTO log_lines ({_LOG_COLUMNS}) VALUES {rows}")
        pending.clear()

    def _apply(self, cursor: Any, kind: str, payload: Any) -> None:
        if kind == "run_started":
            cursor.execute(
                "INSERT OR IGNORE INTO runs (run_id, pipeline_file, pipeline_name, status, trigger,"
                " started_at) VALUES (?, ?, ?, 'running', ?, ?)",
                [payload[0], payload[1], payload[2], payload[3], payload[4]],
            )
        elif kind == "step":
            cursor.execute(
                "INSERT OR REPLACE INTO step_runs (run_id, step_id, type, status, exit_code,"
                " attempts, ignored, started_at, finished_at, duration_s)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(payload),
            )
        elif kind == "run_finished":
            result: RunResult = payload
            cursor.execute(
                "UPDATE runs SET status = ?, finished_at = ?, duration_s = ? WHERE run_id = ?",
                [result.status, result.finished_at, round(result.duration, 3), result.run_id],
            )
            self._line_counts.pop(result.run_id, None)

    def flush(self) -> None:
        """Block until everything queued so far is stored (for tests and consistent reads)."""
        marker = _Flush()
        self._queue.put(marker)
        marker.done.wait(timeout=30)

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._queue.put(_STOP)
        self._writer.join(timeout=30)
        self._con.close()

    # -- reads --------------------------------------------------------------------------
    def _rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self._con.cursor()
        try:
            cursor.execute(sql, params or [])
            names = [d[0] for d in cursor.description or []]
            return [
                {name: _iso(value) for name, value in zip(names, row, strict=True)}
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()

    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        pipeline_file: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if pipeline_file is not None:
            where.append("pipeline_file = ?")
            params.append(pipeline_file)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._rows(
            f"SELECT * FROM runs {clause} ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 1000)), max(0, offset)],
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        runs = self._rows("SELECT * FROM runs WHERE run_id = ?", [run_id])
        if not runs:
            return None
        run = runs[0]
        run["steps"] = self._rows(
            "SELECT * FROM step_runs WHERE run_id = ? ORDER BY started_at NULLS LAST, step_id",
            [run_id],
        )
        return run

    def get_logs(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        levels: list[str] | None = None,
        stream: str | None = None,
        search: str | None = None,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Lines of a run in order. ``levels`` may include ``"NONE"`` for lines without level."""
        where = ["run_id = ?", "seq > ?"]
        params: list[Any] = [run_id, after_seq]
        if step_id is not None:
            where.append("step_id = ?")
            params.append(step_id)
        if stream is not None:
            where.append("stream = ?")
            params.append(stream)
        if levels:
            wanted = [lv.upper() for lv in levels if lv.upper() != "NONE"]
            parts = []
            if wanted:
                parts.append(f"level IN ({','.join('?' * len(wanted))})")
                params += wanted
            if any(lv.upper() == "NONE" for lv in levels):
                parts.append("level IS NULL")
            where.append("(" + " OR ".join(parts) + ")")
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("message ILIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        rows = self._rows(
            f"SELECT {_LOG_COLUMNS} FROM log_lines WHERE {' AND '.join(where)}"
            " ORDER BY seq LIMIT ?",
            [*params, max(1, min(limit, 10_000))],
        )
        for row in rows:
            if isinstance(row["fields"], str):
                row["fields"] = json.loads(row["fields"])
        return rows

    def stats(self) -> dict[str, Any]:
        day_ago = datetime.now() - timedelta(hours=24)
        row = self._rows(
            "SELECT count(*) AS runs_total,"
            " count(*) FILTER (WHERE started_at >= ?) AS runs_24h,"
            " count(*) FILTER (WHERE status = 'running') AS running FROM runs",
            [day_ago],
        )[0]
        recent = self._rows(
            "SELECT status FROM runs WHERE status != 'running' ORDER BY started_at DESC LIMIT 50"
        )
        row["success_rate"] = (
            sum(1 for r in recent if r["status"] == "success") / len(recent) if recent else None
        )
        row["log_lines"] = self._rows("SELECT count(*) AS n FROM log_lines")[0]["n"]
        row["db_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return row

    # -- maintenance --------------------------------------------------------------------
    def purge(
        self, retention_days: int = DEFAULT_RETENTION_DAYS, *, now: datetime | None = None
    ) -> dict[str, int]:
        """Delete runs (with their steps and log lines) older than ``retention_days``."""
        self.flush()
        cutoff = (now or datetime.now()) - timedelta(days=retention_days)
        cursor = self._con.cursor()
        try:
            old = "SELECT run_id FROM runs WHERE started_at < ? AND status != 'running'"
            counts = {}
            for table in ("log_lines", "step_runs"):
                counts[table] = cursor.execute(
                    f"SELECT count(*) FROM {table} WHERE run_id IN ({old})", [cutoff]
                ).fetchone()[0]  # type: ignore[index]
                cursor.execute(f"DELETE FROM {table} WHERE run_id IN ({old})", [cutoff])
            counts["runs"] = cursor.execute(
                "SELECT count(*) FROM runs WHERE started_at < ? AND status != 'running'", [cutoff]
            ).fetchone()[0]  # type: ignore[index]
            cursor.execute(
                "DELETE FROM runs WHERE started_at < ? AND status != 'running'", [cutoff]
            )
        finally:
            cursor.close()
        return counts

    def mark_interrupted(self) -> int:
        """Runs left 'running' by a crash or kill can never finish: mark them failed."""
        self.flush()
        cursor = self._con.cursor()
        try:
            stale = cursor.execute("SELECT count(*) FROM runs WHERE status = 'running'").fetchone()[
                0
            ]  # type: ignore[index]
            cursor.execute("UPDATE step_runs SET status = 'failed' WHERE status = 'running'")
            cursor.execute(
                "UPDATE runs SET status = 'failed', finished_at = ? WHERE status = 'running'",
                [datetime.now()],
            )
        finally:
            cursor.close()
        return int(stale)

    def fail_run(self, run_id: str) -> None:
        """Mark one run (and its unfinished steps) failed after an unexpected crash."""
        self.flush()
        cursor = self._con.cursor()
        try:
            cursor.execute(
                "UPDATE step_runs SET status = 'failed' WHERE run_id = ? AND status = 'running'",
                [run_id],
            )
            cursor.execute(
                "UPDATE runs SET status = 'failed', finished_at = ? WHERE run_id = ?",
                [datetime.now(), run_id],
            )
        finally:
            cursor.close()

    # -- spool ingestion (runs made by `opr run` while the engine was closed) --------------
    def has_run(self, run_id: str) -> bool:
        return bool(self._rows("SELECT 1 AS x FROM runs WHERE run_id = ?", [run_id]))

    def ingest_record(self, record: dict[str, Any], *, trigger: str = "cli") -> bool:
        """Import a run saved by :class:`RunStore`; returns False if it was already known."""
        run_id = str(record["run_id"])
        if self.has_run(run_id):
            return False
        started = datetime.fromisoformat(record["started_at"])
        finished = datetime.fromisoformat(record["finished_at"])
        pipeline_file = record.get("pipeline_file")
        self.run_started(
            run_id,
            record["pipeline"],
            Path(pipeline_file) if pipeline_file else None,
            trigger,
            started,
        )
        steps: list[StepResult] = []
        seq = 0
        for step in record.get("steps", []):
            step_started = _parse_dt(step.get("started_at")) or started
            result = StepResult(
                id=step["id"],
                type=step["type"],
                status=step["status"],
                exit_code=step.get("exit_code"),
                duration=float(step.get("duration") or 0),
                attempts=int(step.get("attempts") or 0),
                ignored=bool(step.get("ignored")),
                started_at=step_started,
                finished_at=_parse_dt(step.get("finished_at")) or finished,
            )
            steps.append(result)
            self.step_finished(run_id, result)
            for stream in ("stdout", "stderr"):
                for text in str(step.get(stream) or "").splitlines():
                    seq += 1
                    level, message, fields = parse_line(text)
                    self.log(
                        LogLine(
                            run_id, result.id, seq, step_started, stream, level, message, fields
                        )
                    )
        self.run_finished(RunResult(run_id, record["pipeline"], started, finished, steps))
        return True

    def ingest_spool(self, directory: Path) -> int:
        """Import every ``.opr/runs/*.json`` under ``directory``; returns how many were new."""
        runs_dir = directory / ".opr" / "runs"
        if not runs_dir.is_dir():
            return 0
        imported = 0
        for path in sorted(runs_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                imported += self.ingest_record(record)
            except (OSError, ValueError, KeyError):
                log.warning("skipping unreadable run file %s", path)
        return imported


def _parse_dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
