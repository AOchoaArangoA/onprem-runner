from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

LOGGER_NAME = "onprem_runner"


class JsonFormatter(logging.Formatter):
    """One JSON object per line; structured fields travel in ``extra={"data": {...}}``."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(*, json_format: bool = False, log_file: Path | None = None) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter: logging.Formatter = (
        JsonFormatter()
        if json_format
        else logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # File logs are always JSON so they can be ingested by other tools.
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)
    handlers[0].setFormatter(formatter)
    for handler in handlers:
        logger.addHandler(handler)
