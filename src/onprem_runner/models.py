from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status = Literal["success", "failed", "timeout", "skipped"]
NotifyWhen = Literal["always", "success", "failure"]
Frequency = Literal["minutes", "hourly", "daily", "weekly", "startup"]
Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_EVERY: dict[str, int] = {"minutes": 1439, "hourly": 23, "daily": 365, "weekly": 52}


class StepConfig(BaseModel):
    """Common step fields; any extra key is a parameter of the step type."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    needs: list[str] = Field(default_factory=list)
    timeout: float | None = Field(default=None, gt=0)
    retries: int = Field(default=0, ge=0)
    retry_delay: float = Field(default=0.0, ge=0)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class NotifierConfig(BaseModel):
    """Common notifier fields; any extra key is a parameter of the notifier type."""

    model_config = ConfigDict(extra="allow")

    type: str
    when: NotifyWhen = "always"

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ScheduleConfig(BaseModel):
    """When Windows Task Scheduler should run the pipeline (see ``opr schedule``)."""

    model_config = ConfigDict(extra="forbid")

    frequency: Frequency
    at: str | None = None
    every: int = Field(default=1, ge=1)
    days: list[Weekday] = Field(default_factory=list)

    @field_validator("at", mode="before")
    @classmethod
    def _at_must_be_text(cls, value: Any) -> Any:
        if isinstance(value, int):  # YAML 1.1 reads an unquoted 8:30 as a number
            raise ValueError('quote the time, e.g. at: "08:30"')
        return value

    @model_validator(mode="after")
    def _check_combination(self) -> ScheduleConfig:
        if self.at is not None and not _TIME.match(self.at):
            raise ValueError('"at" must be HH:MM in 24h format, e.g. "08:30"')
        if self.frequency in ("daily", "weekly") and self.at is None:
            raise ValueError(f'"at" is required for {self.frequency} schedules')
        if self.frequency == "weekly" and not self.days:
            raise ValueError('"days" is required for weekly schedules (e.g. [MON, FRI])')
        if self.frequency != "weekly" and self.days:
            raise ValueError('"days" is only valid for weekly schedules')
        if self.frequency in ("minutes", "hourly", "startup") and self.at is not None:
            raise ValueError(f'"at" is not used by {self.frequency} schedules')
        if self.frequency == "startup" and self.every != 1:
            raise ValueError('"every" is not used by startup schedules')
        limit = _MAX_EVERY.get(self.frequency)
        if limit is not None and self.every > limit:
            raise ValueError(f'"every" must be at most {limit} for {self.frequency} schedules')
        return self

    def describe(self) -> str:
        n = self.every
        match self.frequency:
            case "minutes":
                return "every minute" if n == 1 else f"every {n} minutes"
            case "hourly":
                return "every hour" if n == 1 else f"every {n} hours"
            case "daily":
                return f"{'daily' if n == 1 else f'every {n} days'} at {self.at}"
            case "weekly":
                cadence = "weekly" if n == 1 else f"every {n} weeks"
                return f"{cadence} on {','.join(self.days)} at {self.at}"
            case _:
                return "at system startup"


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    fail_fast: bool = True
    steps: list[StepConfig] = Field(min_length=1)
    notifiers: list[NotifierConfig] = Field(default_factory=list)
    schedule: ScheduleConfig | None = None


@dataclass
class StepResult:
    id: str
    type: str
    status: Status
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    attempts: int = 0


@dataclass
class RunResult:
    run_id: str
    pipeline: str
    started_at: datetime
    finished_at: datetime
    steps: list[StepResult] = field(default_factory=list)

    @property
    def status(self) -> Literal["success", "failed"]:
        return "success" if all(s.status == "success" for s in self.steps) else "failed"

    @property
    def duration(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pipeline": self.pipeline,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration": round(self.duration, 3),
            "steps": [
                {
                    "id": s.id,
                    "type": s.type,
                    "status": s.status,
                    "exit_code": s.exit_code,
                    "duration": round(s.duration, 3),
                    "attempts": s.attempts,
                    "stdout": s.stdout,
                    "stderr": s.stderr,
                }
                for s in self.steps
            ],
        }
