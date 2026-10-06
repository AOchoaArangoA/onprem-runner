from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from onprem_runner.models import RunResult

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
HISTORY_DIRNAME = ".opr"


class RunStore:
    """Keeps one JSON file per run under ``<root>/.opr/runs`` (newest ``keep`` runs)."""

    def __init__(self, root: Path, keep: int = 200) -> None:
        self.runs_dir = root / HISTORY_DIRNAME / "runs"
        self.keep = keep

    def save(self, result: RunResult, pipeline_file: Path | None = None) -> None:
        record = result.to_dict()
        record["pipeline_file"] = str(pipeline_file) if pipeline_file else None
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.runs_dir / f"{result.run_id}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        self._prune()

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Newest first. Step output is omitted; use :meth:`get` for the full record."""
        records: list[dict[str, Any]] = []
        for path in self._files()[:limit]:
            record = self._read(path)
            if record is None:
                continue
            for step in record.get("steps", []):
                step.pop("stdout", None)
                step.pop("stderr", None)
            records.append(record)
        return records

    def get(self, run_id: str) -> dict[str, Any] | None:
        if not RUN_ID_PATTERN.match(run_id):  # also blocks path traversal
            return None
        return self._read(self.runs_dir / f"{run_id}.json")

    def _files(self) -> list[Path]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(self.runs_dir.glob("*.json"), reverse=True)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _prune(self) -> None:
        for old in self._files()[self.keep :]:
            old.unlink(missing_ok=True)
