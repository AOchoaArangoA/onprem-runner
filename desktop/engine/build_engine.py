"""Build the standalone engine (opr-engine.exe) with PyInstaller.

    python desktop/engine/build_engine.py

It uses a clean virtual environment containing only the engine's own dependencies, so the
executable does not drag in whatever happens to be installed on the build machine.
Output: desktop/engine-dist/opr-engine/  (a folder: opr-engine.exe plus its libraries)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_VENV = ROOT / ".build-venv"
WORK = ROOT / "desktop" / "engine" / "build"
OUT = ROOT / "desktop" / "engine-dist"


def run(*args: str | Path) -> None:
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT)


def venv_python() -> Path:
    return BUILD_VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def main() -> None:
    if not BUILD_VENV.exists():
        run(sys.executable, "-m", "venv", BUILD_VENV)
    python = venv_python()
    run(python, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "--quiet", "--upgrade", "pyinstaller", f"{ROOT}[studio]")

    shutil.rmtree(OUT, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    run(
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name",
        "opr-engine",
        "--distpath",
        OUT,
        "--workpath",
        WORK,
        "--specpath",
        WORK,
        "--collect-submodules",
        "onprem_runner",
        "--collect-all",
        "duckdb",
        "--hidden-import",
        "ruamel.yaml",
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "mcp",
        ROOT / "desktop" / "engine" / "entry.py",
    )
    exe = OUT / "opr-engine" / ("opr-engine.exe" if sys.platform == "win32" else "opr-engine")
    size = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file()) / 1_000_000
    print(f"built {exe} ({size:.0f} MB in total)")


if __name__ == "__main__":
    main()
