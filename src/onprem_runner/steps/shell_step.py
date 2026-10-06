from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from onprem_runner.steps.base import (
    ProcessOutcome,
    StepContext,
    StepType,
    run_process,
    shell_encoding,
)


class ShellParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ShellStep(StepType):
    """Run a command line through the system shell (cmd.exe on Windows)."""

    params_model = ShellParams
    params: ShellParams

    def execute(self, ctx: StepContext, timeout: float | None) -> ProcessOutcome:
        cwd = self.resolve(self.params.cwd) if self.params.cwd else self.base_dir
        env = {"ONPREM_RUN_ID": ctx.run_id, **self.params.env}
        return run_process(
            self.params.command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            shell=True,
            encoding=shell_encoding(),
        )
