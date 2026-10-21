from __future__ import annotations

import sys

from pydantic import BaseModel, ConfigDict, Field

from onprem_runner.steps.base import ProcessOutcome, StepContext, StepType, run_process


class PythonParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str
    args: list[str] = Field(default_factory=list)
    python: str | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class PythonStep(StepType):
    """Run a Python script in a subprocess (defaults to the current interpreter)."""

    params_model = PythonParams
    params: PythonParams

    def execute(self, ctx: StepContext, timeout: float | None) -> ProcessOutcome:
        script = self.resolve(self.params.script)
        if not script.is_file():
            message = f"script not found: {script}"
            if ctx.on_line is not None:
                ctx.on_line("stderr", message)
            return ProcessOutcome(-1, "", message)
        cwd = self.resolve(self.params.cwd) if self.params.cwd else self.base_dir
        interpreter = self.params.python or sys.executable
        env = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",  # lines reach the log as they are printed
            "ONPREM_RUN_ID": ctx.run_id,
            **{key: ctx.expand_text(value) for key, value in self.params.env.items()},
        }
        return run_process(
            [interpreter, str(script), *(ctx.expand_text(a) for a in self.params.args)],
            cwd=cwd,
            env=env,
            timeout=timeout,
            on_line=ctx.on_line,
        )
