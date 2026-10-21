from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from onprem_runner.errors import ConfigError
from onprem_runner.models import StepConfig

LineCallback = Callable[[str, str], None]  # (stream: "stdout" | "stderr", text without newline)

MAX_CAPTURED_CHARS = 2_000_000  # per stream kept in memory; lines are still streamed in full
READER_JOIN_SECONDS = 5.0


@dataclass(frozen=True)
class StepContext:
    run_id: str
    base_dir: Path
    step_id: str = ""
    on_line: LineCallback | None = None  # receives every output line as it is produced
    expand: Callable[[str], str] | None = None  # resolves ${secret:NAME} at run time

    def expand_text(self, text: str) -> str:
        return self.expand(text) if self.expand is not None else text


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
    on_line: LineCallback | None = None,
) -> ProcessOutcome:
    """Run a subprocess, streaming its output line by line; never raises for process failures.

    stdin is closed so a script waiting for input fails instead of hanging. When the timeout
    expires the whole process tree is killed, not only the direct child.
    """
    options: dict[str, Any] = {
        "cwd": cwd,
        "env": {**os.environ, **env},
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": shell,
    }
    if sys.platform != "win32":
        options["start_new_session"] = True  # own process group, so it can be killed as a tree
    try:
        proc = subprocess.Popen(cmd, **options)
    except OSError as exc:
        return ProcessOutcome(-1, "", str(exc))

    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}

    def pump(stream: str, pipe: Any) -> None:
        with pipe:
            for raw in iter(pipe.readline, b""):
                text = raw.decode(encoding, errors="replace").rstrip("\r\n")
                if sizes[stream] < MAX_CAPTURED_CHARS:
                    captured[stream].append(text)
                    sizes[stream] += len(text) + 1
                if on_line is not None:
                    with contextlib.suppress(Exception):  # an observer must not kill the step
                        on_line(stream, text)

    readers = [
        threading.Thread(target=pump, args=(name, pipe), daemon=True)
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        proc.wait()
    for reader in readers:  # bounded: a surviving grandchild can keep the pipe open
        reader.join(timeout=READER_JOIN_SECONDS)

    def joined(stream: str) -> str:
        lines = captured[stream]
        return "\n".join(lines) + "\n" if lines else ""

    return ProcessOutcome(
        None if timed_out else proc.returncode,
        joined("stdout"),
        joined("stderr"),
        timed_out=timed_out,
    )


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    if proc.poll() is None:
        proc.kill()


def shell_encoding() -> str:
    return "oem" if sys.platform == "win32" else "utf-8"
