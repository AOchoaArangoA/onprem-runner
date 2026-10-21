"""Persistent application settings (``settings.json`` in the data directory)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from onprem_runner.errors import ConfigError


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["auto", "es", "en"] = "auto"
    theme: Literal["system", "light", "dark"] = "system"
    workspace: str | None = None
    default_python: str | None = None  # None: autodetect / the engine's own interpreter
    retention_days: int = Field(default=90, ge=1, le=3650)
    max_lines_per_run: int = Field(default=200_000, ge=1000, le=10_000_000)
    close_to_tray: bool = True
    start_with_windows: bool = False


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> Settings:
        with self._lock:
            return self._load()

    def update(self, patch: dict[str, Any]) -> Settings:
        """Apply a partial update; unknown or invalid values are rejected as a whole."""
        with self._lock:
            merged = {**self._load().model_dump(), **patch}
            try:
                settings = Settings.model_validate(merged)
            except ValidationError as exc:
                raise ConfigError(f"invalid settings: {exc}") from exc
            self._save(settings)
            return settings

    def _load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            return Settings.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            return Settings()  # a damaged settings file must not stop the app from starting

    def _save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(settings.model_dump(), handle, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
