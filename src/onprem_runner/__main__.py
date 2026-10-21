import runpy
import sys

from onprem_runner.steps.python_step import EMBEDDED_FLAG


def _run_embedded_script() -> None:
    """``engine.exe --opr-run-script script.py args...``: run a script with this interpreter."""
    script, *args = sys.argv[2:]
    sys.argv = [script, *args]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == EMBEDDED_FLAG:
        _run_embedded_script()
    else:
        from onprem_runner.cli import app

        app()
