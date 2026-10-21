"""Where the app keeps its data (portable by default) and where its UI lives."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "OPR Studio"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory):
            pass
    except OSError:
        return False
    return True


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def default_data_dir() -> Path:
    """``OPR_DATA_DIR``; else a ``data`` folder next to the .exe (portable); if that place is
    read-only (for example Program Files) ``%APPDATA%\\OPR Studio``."""
    explicit = os.environ.get("OPR_DATA_DIR")
    if explicit:
        return Path(explicit)
    if is_frozen():
        portable = Path(sys.executable).resolve().parent / "data"
        return portable if is_writable(portable) else appdata_dir()
    return Path.cwd() / "data"


def default_ui_dir() -> Path | None:
    """The built web UI, if one ships with this install."""
    candidates: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "ui")
    if is_frozen():
        candidates.append(Path(sys.executable).resolve().parent / "ui")
    candidates.append(Path(__file__).resolve().parents[2] / "desktop" / "renderer" / "dist")
    return next((c for c in candidates if (c / "index.html").is_file()), None)
