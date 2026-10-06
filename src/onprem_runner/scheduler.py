"""Windows Task Scheduler integration.

Tasks are registered from generated Task XML (``schtasks /Create /XML``): it is
language-neutral and has no 261-character command limit. Status is read with
PowerShell's ``Get-ScheduledTask`` as JSON, because ``schtasks`` output is localised.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import escape

from onprem_runner.models import ScheduleConfig

TASK_FOLDER = "onprem-runner"
_DAY_ELEMENTS = {
    "MON": "Monday",
    "TUE": "Tuesday",
    "WED": "Wednesday",
    "THU": "Thursday",
    "FRI": "Friday",
    "SAT": "Saturday",
    "SUN": "Sunday",
}
_QUERY_SCRIPT = """
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function Fmt($d) { if ($d -and $d.Year -gt 2000) { $d.ToString('s') } else { $null } }
$tasks = Get-ScheduledTask -TaskPath '\\__FOLDER__\\' -ErrorAction SilentlyContinue
$rows = @($tasks | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  [pscustomobject]@{
    name = $_.TaskName; state = [string]$_.State; next_run = Fmt $i.NextRunTime;
    last_run = Fmt $i.LastRunTime; last_result = $i.LastTaskResult
  }
})
ConvertTo-Json -InputObject $rows -Compress
""".replace("__FOLDER__", TASK_FOLDER)


class SchedulerError(Exception):
    """schtasks/PowerShell failed or the platform is unsupported."""


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    state: str
    next_run: str | None
    last_run: str | None
    last_result: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_result": self.last_result,
        }


class SchedulerBackend(Protocol):
    @property
    def available(self) -> bool: ...

    def install(
        self, pipeline_file: Path, name: str, schedule: ScheduleConfig, *, system: bool = False
    ) -> str: ...
    def remove(self, pipeline_file: Path, name: str) -> bool: ...
    def list_tasks(self) -> dict[str, ScheduledTask]: ...


def task_name(pipeline_file: Path, name: str) -> str:
    """Stable task name: readable slug plus a short hash of the file path."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:40] or "pipeline"
    digest = hashlib.sha1(str(pipeline_file.resolve()).lower().encode()).hexdigest()[:6]
    return f"{slug}-{digest}"


def build_task_xml(
    schedule: ScheduleConfig,
    pipeline_file: Path,
    *,
    python: str | None = None,
    system: bool = False,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    pipeline_file = pipeline_file.resolve()
    arguments = f'-m onprem_runner run "{pipeline_file}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(f"onprem-runner: {pipeline_file}")}</Description>
  </RegistrationInfo>
  <Triggers>{_trigger_xml(schedule, now)}</Triggers>
  <Principals>
    <Principal id="Author">{_principal_xml(system)}</Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python or sys.executable)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(pipeline_file.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _principal_xml(system: bool) -> str:
    if system:  # runs without a logged-in user; needs an elevated shell to register
        return "<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>"
    return "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>"


def _trigger_xml(schedule: ScheduleConfig, now: datetime) -> str:
    if schedule.frequency == "startup":
        return "<BootTrigger><Enabled>true</Enabled></BootTrigger>"

    if schedule.frequency in ("minutes", "hourly"):
        interval = (
            f"PT{schedule.every}M" if schedule.frequency == "minutes" else f"PT{schedule.every}H"
        )
        start = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        repetition = f"<Repetition><Interval>{interval}</Interval></Repetition>"
        recurrence = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    else:
        hour, minute = (schedule.at or "00:00").split(":")
        start = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        repetition = ""
        if schedule.frequency == "daily":
            recurrence = (
                f"<ScheduleByDay><DaysInterval>{schedule.every}</DaysInterval></ScheduleByDay>"
            )
        else:
            days = "".join(f"<{_DAY_ELEMENTS[d]}/>" for d in schedule.days)
            recurrence = (
                f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek>"
                f"<WeeksInterval>{schedule.every}</WeeksInterval></ScheduleByWeek>"
            )
    return (
        f"<CalendarTrigger>{repetition}<StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary>"
        f"<Enabled>true</Enabled>{recurrence}</CalendarTrigger>"
    )


class TaskScheduler:
    """Real backend; only works on Windows."""

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def install(
        self, pipeline_file: Path, name: str, schedule: ScheduleConfig, *, system: bool = False
    ) -> str:
        self._require_windows()
        full_name = f"{TASK_FOLDER}\\{task_name(pipeline_file, name)}"
        xml = build_task_xml(schedule, pipeline_file, system=system)
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as handle:
                handle.write(xml)
            self._schtasks(["/Create", "/TN", full_name, "/XML", tmp, "/F"])
        finally:
            Path(tmp).unlink(missing_ok=True)
        return full_name

    def remove(self, pipeline_file: Path, name: str) -> bool:
        self._require_windows()
        full_name = f"{TASK_FOLDER}\\{task_name(pipeline_file, name)}"
        if task_name(pipeline_file, name) not in self.list_tasks():
            return False
        self._schtasks(["/Delete", "/TN", full_name, "/F"])
        return True

    def list_tasks(self) -> dict[str, ScheduledTask]:
        self._require_windows()
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY_SCRIPT],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError(f"cannot query Task Scheduler: {exc}") from exc
        if completed.returncode != 0:
            raise SchedulerError(completed.stderr.decode("utf-8", "replace").strip())
        text = completed.stdout.decode("utf-8-sig", "replace").strip()
        rows = json.loads(text) if text else []
        if isinstance(rows, dict):  # PowerShell unwraps single-item arrays in some versions
            rows = [rows]
        return {
            r["name"]: ScheduledTask(
                r["name"], r["state"], r["next_run"], r["last_run"], r["last_result"]
            )
            for r in rows
        }

    @staticmethod
    def _require_windows() -> None:
        if sys.platform != "win32":
            raise SchedulerError("Task Scheduler integration is only available on Windows")

    @staticmethod
    def _schtasks(args: list[str]) -> None:
        try:
            completed = subprocess.run(
                ["schtasks", *args], capture_output=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError(f"schtasks failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).decode("oem", "replace").strip()
            raise SchedulerError(f"schtasks {args[0]} failed: {detail}")
