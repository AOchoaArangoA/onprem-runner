from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A directory with scripts that succeed, fail and hang."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "ok.py").write_text('print("ok")\n', encoding="utf-8")
    (scripts / "fail.py").write_text(
        'import sys\nprint("boom", file=sys.stderr)\nsys.exit(3)\n', encoding="utf-8"
    )
    (scripts / "slow.py").write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    return tmp_path


def write_pipeline(project: Path, body: str) -> Path:
    path = project / "pipeline.yaml"
    path.write_text(body, encoding="utf-8")
    return path
