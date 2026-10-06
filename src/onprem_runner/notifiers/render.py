from __future__ import annotations

import csv
import io
import json

from onprem_runner.models import RunResult


def render_json(result: RunResult) -> str:
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


def render_csv(result: RunResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["step_id", "type", "status", "exit_code", "duration_s", "attempts"])
    for s in result.steps:
        writer.writerow([s.id, s.type, s.status, s.exit_code, round(s.duration, 3), s.attempts])
    return buffer.getvalue()


def render_text(result: RunResult) -> str:
    lines = [
        f"Pipeline: {result.pipeline}",
        f"Run: {result.run_id}",
        f"Status: {result.status.upper()}",
        f"Duration: {result.duration:.2f}s",
        "",
    ]
    for s in result.steps:
        lines.append(f"- {s.id} [{s.type}] {s.status} (exit={s.exit_code}, {s.duration:.2f}s)")
        if s.status != "success" and s.stderr.strip():
            lines.append("    stderr: " + s.stderr.strip().replace("\n", "\n            "))
    return "\n".join(lines) + "\n"
