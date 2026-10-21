from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status = Literal["success", "failed", "timeout", "skipped", "cancelled"]
NotifyWhen = Literal["always", "success", "failure"]
EdgeWhen = Literal["success", "failure", "always"]
Frequency = Literal["minutes", "hourly", "daily", "weekly", "startup"]
Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_EVERY: dict[str, int] = {"minutes": 1439, "hourly": 23, "daily": 365, "weekly": 52}


class Edge(BaseModel):
    """A dependency: run only after ``step`` finished and its outcome matches ``when``.

    ``success`` (default) needs the step to succeed; ``failure`` needs it to fail or time out;
    ``always`` runs after it whatever happened, even if it was skipped.
    """

    model_config = ConfigDict(extra="forbid")

    step: str
    when: EdgeWhen = "success"


class StepConfig(BaseModel):
    """Common step fields; any extra key is a parameter of the step type."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    needs: list[Edge] = Field(default_factory=list)
    timeout: float | None = Field(default=None, gt=0)
    retries: int = Field(default=0, ge=0)
    retry_delay: float = Field(default=0.0, ge=0)
    ignore_failure: bool = False

    @field_validator("needs", mode="before")
    @classmethod
    def _plain_ids_mean_success(cls, value: Any) -> Any:
        """Accept ``needs: [a, b]`` as shorthand for ``when: success`` edges."""
        if isinstance(value, list):
            return [{"step": v} if isinstance(v, str) else v for v in value]
        return value

    @property
    def needs_ids(self) -> list[str]:
        return [edge.step for edge in self.needs]

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
    max_parallel: int = Field(default=4, ge=1, le=64)
    steps: list[StepConfig] = Field(min_length=1)
    notifiers: list[NotifierConfig] = Field(default_factory=list)
    schedule: ScheduleConfig | None = None
    ui: UiConfig | None = None


class UiConfig(BaseModel):
    """Editor-only data stored with the pipeline (node positions on the canvas)."""

    model_config = ConfigDict(extra="allow")

    positions: dict[str, list[float]] = Field(default_factory=dict)

    @field_validator("positions")
    @classmethod
    def _positions_are_pairs(cls, value: dict[str, list[float]]) -> dict[str, list[float]]:
        for step, pos in value.items():
            if len(pos) != 2:
                raise ValueError(f"position of {step!r} must be [x, y]")
        return value


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
    ignored: bool = False  # a failure that does not count towards the run status
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class RunResult:
    run_id: str
    pipeline: str
    started_at: datetime
    finished_at: datetime
    steps: list[StepResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def status(self) -> Literal["success", "failed", "cancelled"]:
        """Failed if any step failed or timed out (unless ignored). Skipped steps are normal:
        a ``when: failure`` branch is skipped on success."""
        if self.cancelled:
            return "cancelled"
        broken = any(s.status in ("failed", "timeout") and not s.ignored for s in self.steps)
        return "failed" if broken else "success"

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
                    "ignored": s.ignored,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                    "stdout": s.stdout,
                    "stderr": s.stderr,
                }
                for s in self.steps
            ],
        }
