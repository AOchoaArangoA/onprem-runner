from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

from onprem_runner.config import execution_order
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.models import PipelineConfig, RunResult, StepConfig, StepResult
from onprem_runner.notifiers import Notifier
from onprem_runner.registry import get_notifier_type, get_step_type
from onprem_runner.steps import StepContext, StepType
from onprem_runner.store import RunStore

log = logging.getLogger(LOGGER_NAME)

# (event, step_id, result): event is "step_started", "step_finished" or "step_skipped".
EventCallback = Callable[[str, str, StepResult | None], None]


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class Runner:
    """Executes a pipeline's steps in dependency order, then dispatches notifiers.

    Plugin parameters are validated in the constructor, so a bad configuration
    fails before any step runs.
    """

    def __init__(
        self,
        config: PipelineConfig,
        base_dir: Path,
        *,
        store: RunStore | None = None,
        pipeline_file: Path | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.store = store
        self.pipeline_file = pipeline_file
        self.on_event = on_event
        self._steps: list[tuple[StepConfig, StepType]] = [
            (step, get_step_type(step.type)(step, base_dir))
            for step in execution_order(config.steps)
        ]
        self._notifiers: list[Notifier] = [
            get_notifier_type(n.type)(n, base_dir) for n in config.notifiers
        ]

    def run(self, run_id: str | None = None) -> RunResult:
        run_id = run_id or new_run_id()
        ctx = StepContext(run_id=run_id, base_dir=self.base_dir)
        started = datetime.now()
        log.info("run started", extra={"data": {"run_id": run_id, "pipeline": self.config.name}})

        results = self._schedule(ctx)

        run_result = RunResult(
            run_id=run_id,
            pipeline=self.config.name,
            started_at=started,
            finished_at=datetime.now(),
            steps=[results[cfg.id] for cfg, _ in self._steps],
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
        self._save(run_result)
        self._notify(run_result)
        return run_result

    def _schedule(self, ctx: StepContext) -> dict[str, StepResult]:
        """Run steps as their dependencies settle, at most ``max_parallel`` at a time.

        A step is *ready* once every step it needs has settled (finished or skipped). It then
        runs only if every edge condition holds; otherwise it is skipped. With ``fail_fast``,
        after a real failure only recovery steps (those with a ``failure``/``always`` edge) run.
        """
        results: dict[str, StepResult] = {}
        pending = [cfg for cfg, _ in self._steps]
        instances = {cfg.id: step for cfg, step in self._steps}
        running: dict[Future[StepResult], StepConfig] = {}
        failed_any = False

        with ThreadPoolExecutor(max_workers=self.config.max_parallel) as pool:
            while pending or running:
                progressed = False
                for cfg in list(pending):
                    if not all(dep in results for dep in cfg.needs_ids):
                        continue
                    if self._should_skip(cfg, results, failed_any):
                        pending.remove(cfg)
                        progressed = True
                        results[cfg.id] = StepResult(id=cfg.id, type=cfg.type, status="skipped")
                        self._emit("step_skipped", cfg.id, results[cfg.id])
                        log.warning(
                            "step skipped", extra={"data": {"run_id": ctx.run_id, "step": cfg.id}}
                        )
                    elif len(running) < self.config.max_parallel:
                        # Submit only into a free slot so fail_fast can still cancel what
                        # has not started yet.
                        pending.remove(cfg)
                        progressed = True
                        running[pool.submit(self._run_step, cfg, instances[cfg.id], ctx)] = cfg
                if progressed:
                    continue  # a skip may have made other steps ready
                if not running:
                    raise RuntimeError(f"unschedulable steps: {[c.id for c in pending]}")
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    cfg = running.pop(future)
                    results[cfg.id] = future.result()
                    failed_any = failed_any or (
                        results[cfg.id].status in ("failed", "timeout")
                        and not results[cfg.id].ignored
                    )
        return results

    def _should_skip(
        self, cfg: StepConfig, results: dict[str, StepResult], failed_any: bool
    ) -> bool:
        for edge in cfg.needs:
            status = results[edge.step].status
            if edge.when == "success" and status != "success":
                return True
            if edge.when == "failure" and status not in ("failed", "timeout"):
                return True
        is_recovery = any(edge.when in ("failure", "always") for edge in cfg.needs)
        return self.config.fail_fast and failed_any and not is_recovery

    def _run_step(self, cfg: StepConfig, step: StepType, ctx: StepContext) -> StepResult:
        self._emit("step_started", cfg.id, None)
        result = self._execute(cfg, step, ctx)
        result.ignored = cfg.ignore_failure and result.status in ("failed", "timeout")
        self._emit("step_finished", cfg.id, result)
        return result

    def _emit(self, event: str, step_id: str, result: StepResult | None) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, step_id, result)
        except Exception:  # observers must never break a run
            log.exception("event callback failed")

    def _save(self, run_result: RunResult) -> None:
        if self.store is None:
            return
        try:
            self.store.save(run_result, self.pipeline_file)
        except OSError as exc:
            log.error("could not save run history", extra={"data": {"error": str(exc)}})

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
