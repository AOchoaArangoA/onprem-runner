"""Find Python interpreters that steps can run with."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT = 10.0
_VERSION = re.compile(r"Python (\d+\.\d+\.\d+)")
SKIP_DIRS = {"node_modules", "site-packages", "__pycache__", ".git", "dist", "build"}


@dataclass(frozen=True)
class Interpreter:
    path: str
    version: str | None
    source: str  # "py-launcher" | "path" | "venv" | "engine" | "custom"

    def to_dict(self) -> dict[str, str | None]:
        return {"path": self.path, "version": self.version, "source": self.source}


def probe(path: str) -> str | None:
    """Return the interpreter's version (``"3.11.9"``) or ``None`` if it does not run."""
    try:
        completed = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION.search(completed.stdout + completed.stderr)
    return match.group(1) if completed.returncode == 0 and match else None


def _py_launcher_paths() -> list[str]:
    if sys.platform != "win32" or shutil.which("py") is None:
        return []
    try:
        out = subprocess.run(
            ["py", "-0p"], capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    # Lines look like " -3.11-64        C:\\Python311\\python.exe" (with an optional "*").
    return [m.group(1) for m in re.finditer(r"([A-Za-z]:\\[^\r\n]*python\w*\.exe)", out)]


def _venv_paths(workspace: Path | None, depth: int = 3) -> list[str]:
    if workspace is None or not workspace.is_dir():
        return []
    suffix = Path("Scripts/python.exe") if sys.platform == "win32" else Path("bin/python")
    found: list[str] = []
    root_depth = len(workspace.parts)
    for current, dirs, _ in os.walk(workspace):
        level = len(Path(current).parts) - root_depth
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and level < depth]
        candidate = Path(current) / suffix
        if (Path(current) / "pyvenv.cfg").exists() and candidate.exists():
            found.append(str(candidate))
    return found


def detect(workspace: Path | None = None) -> list[Interpreter]:
    """Interpreters found on this machine, without duplicates, most specific first."""
    candidates: list[tuple[str, str]] = [(p, "venv") for p in _venv_paths(workspace)]
    candidates += [(p, "py-launcher") for p in _py_launcher_paths()]
    for name in ("python", "python3"):
        located = shutil.which(name)
        if located:
            candidates.append((located, "path"))
    # Not the packaged engine itself: it only runs scripts through `embedded`, not as a python.exe.

    seen: set[str] = set()
    result: list[Interpreter] = []
    for path, source in candidates:
        key = os.path.normcase(os.path.realpath(path))
        if key in seen or "WindowsApps" in path:  # Store stubs open the Store instead of running
            continue
        seen.add(key)
        version = probe(path)
        if version is not None:
            result.append(Interpreter(path, version, source))
    return result
