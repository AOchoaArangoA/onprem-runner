from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["success", "failed", "timeout", "skipped"]
NotifyWhen = Literal["always", "success", "failure"]


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


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    fail_fast: bool = True
    steps: list[StepConfig] = Field(min_length=1)
    notifiers: list[NotifierConfig] = Field(default_factory=list)


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
