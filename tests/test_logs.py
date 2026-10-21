from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import write_pipeline
from onprem_runner.config import load_config
from onprem_runner.logs import LogLine, parse_line
from onprem_runner.runner import Runner

# -- parse_line ---------------------------------------------------------------------


def test_plain_text_has_no_level() -> None:
    assert parse_line("hello world") == (None, "hello world", None)


def test_structured_line_extracts_level_message_and_fields() -> None:
    level, message, fields = parse_line('{"level": "warn", "msg": "slow query", "ms": 1200}')
    assert (level, message, fields) == ("WARNING", "slow query", {"ms": 1200})


@pytest.mark.parametrize(
    "line",
    [
        '{"ms": 12}',  # JSON but no message key: plain text
        "{not json}",
        "[1, 2, 3]",
        '"just a string"',
    ],
)
def test_json_without_message_stays_plain(line: str) -> None:
    assert parse_line(line) == (None, line, None)


def test_message_without_level_is_structured_but_levelless() -> None:
    assert parse_line('{"message": "hi", "user": "ana"}') == (None, "hi", {"user": "ana"})


# -- streaming ------------------------------------------------------------------


def collect(project: Path, body: str, **runner_kwargs: object) -> tuple[list[LogLine], object]:
    lines: list[LogLine] = []
    runner = Runner(
        load_config(write_pipeline(project, body)),
        project,
        on_log=lines.append,
        **runner_kwargs,  # type: ignore[arg-type]
    )
    return lines, runner.run()


def test_lines_are_streamed_with_stream_step_and_order(project: Path) -> None:
    (project / "scripts" / "talk.py").write_text(
        "import sys\n"
        "print('one')\n"
        "print('oops', file=sys.stderr)\n"
        'print(\'{"level": "error", "msg": "bad", "code": 7}\')\n',
        encoding="utf-8",
    )
    lines, _ = collect(
        project, "name: p\nsteps:\n  - {id: talk, type: python, script: scripts/talk.py}\n"
    )
    by_message = {line.message: line for line in lines}
    assert by_message["one"].stream == "stdout"
    assert by_message["oops"].stream == "stderr"
    assert by_message["bad"].level == "ERROR"
    assert by_message["bad"].fields == {"code": 7}
    assert {line.step_id for line in lines} == {"talk"}
    assert [line.seq for line in lines] == sorted(line.seq for line in lines)
    assert len({line.seq for line in lines}) == len(lines)


def test_output_arrives_while_the_step_is_still_running(project: Path) -> None:
    (project / "scripts" / "slow_talk.py").write_text(
        "import time\nprint('early')\ntime.sleep(2)\nprint('late')\n", encoding="utf-8"
    )
    seen_at: dict[str, float] = {}

    def on_log(line: LogLine) -> None:
        seen_at[line.message] = time.monotonic()

    runner = Runner(
        load_config(
            write_pipeline(
                project,
                "name: p\nsteps:\n  - {id: s, type: python, script: scripts/slow_talk.py}\n",
            )
        ),
        project,
        on_log=on_log,
    )
    runner.run()
    assert seen_at["late"] - seen_at["early"] > 1.0  # 'early' was not held until exit


def test_seq_is_unique_across_parallel_steps(project: Path) -> None:
    (project / "scripts" / "burst.py").write_text(
        "for i in range(50):\n    print(i)\n", encoding="utf-8"
    )
    lines, _ = collect(
        project,
        "name: p\nsteps:\n"
        "  - {id: a, type: python, script: scripts/burst.py}\n"
        "  - {id: b, type: python, script: scripts/burst.py}\n",
    )
    assert len(lines) == 100
    assert len({line.seq for line in lines}) == 100


def test_redactor_masks_logs_and_stored_output(project: Path) -> None:
    (project / "scripts" / "leak.py").write_text("print('token=SECRET123')\n", encoding="utf-8")
    lines, result = collect(
        project,
        "name: p\nsteps:\n  - {id: leak, type: python, script: scripts/leak.py}\n",
        redact=lambda text: text.replace("SECRET123", "***"),
    )
    assert [line.message for line in lines] == ["token=***"]
    assert "SECRET123" not in result.steps[0].stdout  # type: ignore[attr-defined]


def test_stdin_is_closed_so_input_does_not_hang(project: Path) -> None:
    (project / "scripts" / "ask.py").write_text("input('name? ')\n", encoding="utf-8")
    started = time.monotonic()
    _, result = collect(
        project,
        "name: p\nsteps:\n  - {id: ask, type: python, script: scripts/ask.py, timeout: 20}\n",
    )
    assert time.monotonic() - started < 10
    assert result.steps[0].status == "failed"  # type: ignore[attr-defined]


# -- timeout kills the whole tree -------------------------------------------------


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in out
    try:
        import os

        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_timeout_kills_grandchildren_too(project: Path) -> None:
    (project / "scripts" / "spawner.py").write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    _, result = collect(
        project,
        "name: p\nsteps:\n  - {id: sp, type: python, script: scripts/spawner.py, timeout: 2}\n",
    )
    assert result.steps[0].status == "timeout"  # type: ignore[attr-defined]
    child_pid = int((project / "child.pid").read_text())
    for _ in range(30):  # the OS needs a moment to reap it
        if not pid_alive(child_pid):
            break
        time.sleep(0.2)
    assert not pid_alive(child_pid), "grandchild survived the timeout"
