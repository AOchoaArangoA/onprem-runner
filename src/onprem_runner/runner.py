from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from onprem_runner.config import execution_order
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.models import PipelineConfig, RunResult, StepConfig, StepResult
from onprem_runner.notifiers import Notifier
from onprem_runner.registry import get_notifier_type, get_step_type
from onprem_runner.steps import StepContext, StepType

log = logging.getLogger(LOGGER_NAME)


class Runner:
    """Executes a pipeline's steps in dependency order, then dispatches notifiers.

    Plugin parameters are validated in the constructor, so a bad configuration
    fails before any step runs.
    """

    def __init__(self, config: PipelineConfig, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        self._steps: list[tuple[StepConfig, StepType]] = [
            (step, get_step_type(step.type)(step, base_dir))
            for step in execution_order(config.steps)
        ]
        self._notifiers: list[Notifier] = [
            get_notifier_type(n.type)(n, base_dir) for n in config.notifiers
        ]

    def run(self) -> RunResult:
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        ctx = StepContext(run_id=run_id, base_dir=self.base_dir)
        started = datetime.now()
        log.info("run started", extra={"data": {"run_id": run_id, "pipeline": self.config.name}})

        results: dict[str, StepResult] = {}
        failed_any = False
        for cfg, step in self._steps:
            blocked = any(results[dep].status != "success" for dep in cfg.needs)
            if blocked or (self.config.fail_fast and failed_any):
                result = StepResult(id=cfg.id, type=cfg.type, status="skipped")
                log.warning("step skipped", extra={"data": {"run_id": run_id, "step": cfg.id}})
            else:
                result = self._execute(cfg, step, ctx)
                failed_any = failed_any or result.status != "success"
            results[cfg.id] = result

        run_result = RunResult(
            run_id=run_id,
            pipeline=self.config.name,
            started_at=started,
            finished_at=datetime.now(),
            steps=list(results.values()),
        )
        log.info(
            "run finished",
            extra={
                "data": {
                    "run_id": run_id,
                    "pipeline": self.config.name,
                    "status": run_result.status,
                    "duration": round(run_result.duration, 3),
                }
            },
        )
        self._notify(run_result)
        return run_result

    def _execute(self, cfg: StepConfig, step: StepType, ctx: StepContext) -> StepResult:
        result = StepResult(id=cfg.id, type=cfg.type, status="failed")
        for attempt in range(1, cfg.retries + 2):
            result.attempts = attempt
            start = time.monotonic()
            try:
                outcome = step.execute(ctx, cfg.timeout)
                result.exit_code = outcome.exit_code
                result.stdout, result.stderr = outcome.stdout, outcome.stderr
                if outcome.timed_out:
                    result.status = "timeout"
                else:
                    result.status = "success" if outcome.exit_code == 0 else "failed"
            except Exception as exc:  # a plugin bug must not abort the whole run
                result.status, result.exit_code = "failed", None
                result.stderr = f"{type(exc).__name__}: {exc}"
            result.duration = time.monotonic() - start
            log.info(
                "step finished",
                extra={
                    "data": {
                        "run_id": ctx.run_id,
                        "step": cfg.id,
                        "status": result.status,
                        "exit_code": result.exit_code,
                        "attempt": attempt,
                        "duration": round(result.duration, 3),
                    }
                },
            )
            if result.status == "success":
                break
            if attempt <= cfg.retries and cfg.retry_delay:
                time.sleep(cfg.retry_delay)
        return result

    def _notify(self, run_result: RunResult) -> None:
        for notifier in self._notifiers:
            if not notifier.should_notify(run_result):
                continue
            try:
                notifier.notify(run_result)
            except Exception as exc:  # delivery problems never change the run outcome
                log.error(
                    "notifier failed",
                    extra={
                        "data": {
                            "run_id": run_result.run_id,
                            "notifier": notifier.config.type,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    },
                )
