from __future__ import annotations

import os
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from onprem_runner.errors import ConfigError
from onprem_runner.models import StepConfig


@dataclass(frozen=True)
class StepContext:
    run_id: str
    base_dir: Path


@dataclass
class ProcessOutcome:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class StepType(ABC):
    """Base class for step types. Subclass and register under the
    ``onprem_runner.steps`` entry-point group to add new ones."""

    params_model: ClassVar[type[BaseModel]]

    def __init__(self, config: StepConfig, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        try:
            self.params = self.params_model(**config.params)
        except ValidationError as exc:
            raise ConfigError(f"step {config.id!r} ({config.type}): {exc}") from exc

    @abstractmethod
    def execute(self, ctx: StepContext, timeout: float | None) -> ProcessOutcome: ...

    def resolve(self, path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.base_dir / candidate


def run_process(
    cmd: str | list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float | None,
    shell: bool = False,
    encoding: str = "utf-8",
) -> ProcessOutcome:
    """Run a subprocess capturing output; never raises for process failures."""
    full_env = {**os.environ, **env}

    def decode(data: bytes | str | None) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        return data.decode(encoding, errors="replace")

    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=full_env,
            capture_output=True,
            timeout=timeout,
            shell=shell,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessOutcome(None, decode(exc.stdout), decode(exc.stderr), timed_out=True)
    except OSError as exc:
        return ProcessOutcome(-1, "", str(exc))
    return ProcessOutcome(completed.returncode, decode(completed.stdout), decode(completed.stderr))


def shell_encoding() -> str:
    return "oem" if sys.platform == "win32" else "utf-8"
