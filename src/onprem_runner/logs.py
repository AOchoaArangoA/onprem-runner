"""Log lines captured from step output, with optional structured (JSON) fields."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_MESSAGE_KEYS = ("msg", "message", "event")
_LEVEL_KEYS = ("level", "levelname", "severity")
_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL", "ERR": "ERROR", "TRACE": "DEBUG"}


@dataclass(frozen=True)
class LogLine:
    run_id: str
    step_id: str
    seq: int  # order within the run, across all steps
    ts: datetime
    stream: str  # "stdout" | "stderr"
    level: str | None
    message: str
    fields: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "stream": self.stream,
            "level": self.level,
            "message": self.message,
            "fields": self.fields,
        }


def parse_line(text: str) -> tuple[str | None, str, dict[str, Any] | None]:
    """Return ``(level, message, fields)``.

    A line that is a JSON object with a ``msg``/``message``/``event`` key is structured:
    its level and the remaining keys are kept. Anything else is plain text with no level.
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None, text, None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None, text, None
    if not isinstance(data, dict):
        return None, text, None
    message_key = next((k for k in _MESSAGE_KEYS if k in data), None)
    if message_key is None:
        return None, text, None
    level_key = next((k for k in _LEVEL_KEYS if k in data), None)
    level = None
    if level_key is not None and data[level_key] is not None:
        level = str(data[level_key]).strip().upper()
        level = _LEVEL_ALIASES.get(level, level)
    fields = {k: v for k, v in data.items() if k not in (message_key, level_key)}
    return level or None, str(data[message_key]), fields or None
