from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Collection
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from onprem_runner.config import execution_order
from onprem_runner.errors import ConfigError
from onprem_runner.logging_utils import LOGGER_NAME
from onprem_runner.logs import LogLine, parse_line
from onprem_runner.models import PipelineConfig, RunResult, StepConfig, StepResult
from onprem_runner.notifiers import Notifier
from onprem_runner.registry import get_notifier_type, get_step_type
from onprem_runner.secrets_store import REFERENCE, SecretError, SecretStore
from onprem_runner.steps import StepContext, StepType
from onprem_runner.store import RunStore

log = logging.getLogger(LOGGER_NAME)

# (event, step_id, result): event is "step_started", "step_finished" or "step_skipped".
EventCallback = Callable[[str, str, StepResult | None], None]
LogCallback = Callable[[LogLine], None]
Redactor = Callable[[str], str]


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class RunRecorder(Protocol):
    """Receives everything that happens in a run (implemented by the DuckDB layer).

    Calls must be cheap and non-blocking: they happen on the step threads.
    """

    def run_started(
        self,
        run_id: str,
        pipeline_name: str,
        pipeline_file: Path | None,
        trigger: str,
        started_at: datetime,
    ) -> None: ...
    def step_started(
        self, run_id: str, step_id: str, step_type: str, started_at: datetime
    ) -> None: ...
    def step_finished(self, run_id: str, result: StepResult) -> None: ...
    def log(self, line: LogLine) -> None: ...
    def run_finished(self, result: RunResult) -> None: ...


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
        on_log: LogCallback | None = None,
        redact: Redactor | None = None,
        recorder: RunRecorder | None = None,
        secrets: SecretStore | None = None,
        default_python: str | None = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.store = store
        self.pipeline_file = pipeline_file
        self.on_event = on_event
        self.on_log = on_log
        self.redact = redact
        self.recorder = recorder
        self.secrets = secrets
        self.default_python = default_python
        self._redact: Redactor | None = redact
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._cancel = threading.Event()
        self._steps: list[tuple[StepConfig, StepType]] = [
            (step, get_step_type(step.type)(step, base_dir))
            for step in execution_order(config.steps)
        ]
        self._notifiers: list[Notifier] = [
            get_notifier_type(n.type)(n, base_dir) for n in config.notifiers
        ]

    def cancel(self) -> None:
        """Stop the current run: running steps are killed, pending steps are skipped."""
        self._cancel.set()

    def run(
        self,
        run_id: str | None = None,
        trigger: str = "cli",
        only: Collection[str] | None = None,
    ) -> RunResult:
        """Execute the pipeline. With ``only``, run just those steps; edges to steps outside
        the selection are treated as satisfied."""
        known = {cfg.id for cfg, _ in self._steps}
        if only is not None and not set(only) <= known:
            raise ConfigError(f"unknown steps: {sorted(set(only) - known)}")
        selected = set(only) if only is not None else known
        self._cancel = threading.Event()
        run_id = run_id or new_run_id()
        self._redact = self.redact or (self.secrets.redactor() if self.secrets else None)
        ctx = StepContext(
            run_id=run_id,
            base_dir=self.base_dir,
            expand=self._expand,
            cancel=self._cancel,
            default_python=self.default_python,
        )
        self._seq = 0
        started = datetime.now()
        log.info("run started", extra={"data": {"run_id": run_id, "pipeline": self.config.name}})
        self._record(
            lambda r: r.run_started(run_id, self.config.name, self.pipeline_file, trigger, started)
        )

        results = self._schedule(ctx, selected)

        run_result = RunResult(
            run_id=run_id,
            pipeline=self.config.name,
            started_at=started,
            finished_at=datetime.now(),
            steps=[results[cfg.id] for cfg, _ in self._steps if cfg.id in results],
            cancelled=self._cancel.is_set(),
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
        self._record(lambda r: r.run_finished(run_result))
        self._save(run_result)
        self._notify(run_result)
        return run_result

    def _schedule(self, ctx: StepContext, selected: set[str]) -> dict[str, StepResult]:
        """Run steps as their dependencies settle, at most ``max_parallel`` at a time.

        A step is *ready* once every step it needs has settled (finished or skipped). It then
        runs only if every edge condition holds; otherwise it is skipped. With ``fail_fast``,
        after a real failure only recovery steps (those with a ``failure``/``always`` edge) run.
        """
        results: dict[str, StepResult] = {}
        pending = [cfg for cfg, _ in self._steps if cfg.id in selected]
        instances = {cfg.id: step for cfg, step in self._steps}
        running: dict[Future[StepResult], StepConfig] = {}
        failed_any = False

        with ThreadPoolExecutor(max_workers=self.config.max_parallel) as pool:
            while pending or running:
                progressed = False
                for cfg in list(pending):
                    if not all(dep in results for dep in cfg.needs_ids if dep in selected):
                        continue
                    if self._cancel.is_set() or self._should_skip(cfg, results, failed_any):
                        pending.remove(cfg)
                        progressed = True
                        skipped = StepResult(id=cfg.id, type=cfg.type, status="skipped")
                        results[cfg.id] = skipped
                        self._emit("step_skipped", cfg.id, skipped)
                        self._record_step_finished(ctx.run_id, skipped)
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
            if edge.step not in results:
                continue  # outside a partial run: treated as satisfied
            status = results[edge.step].status
            if edge.when == "success" and status != "success":
                return True
            if edge.when == "failure" and status not in ("failed", "timeout"):
                return True
        is_recovery = any(edge.when in ("failure", "always") for edge in cfg.needs)
        return self.config.fail_fast and failed_any and not is_recovery

    def _run_step(self, cfg: StepConfig, step: StepType, ctx: StepContext) -> StepResult:
        if self._cancel.is_set():
            skipped = StepResult(id=cfg.id, type=cfg.type, status="skipped")
            self._emit("step_skipped", cfg.id, skipped)
            self._record_step_finished(ctx.run_id, skipped)
            return skipped
        self._emit("step_started", cfg.id, None)
        self._record(lambda r: r.step_started(ctx.run_id, cfg.id, cfg.type, datetime.now()))
        step_ctx = replace(ctx, step_id=cfg.id, on_line=self._line_sink(ctx.run_id, cfg.id))
        result = self._execute(cfg, step, step_ctx)
        result.ignored = cfg.ignore_failure and result.status in ("failed", "timeout")
        self._emit("step_finished", cfg.id, result)
        self._record_step_finished(ctx.run_id, result)
        return result

    def _record_step_finished(self, run_id: str, result: StepResult) -> None:
        self._record(lambda r: r.step_finished(run_id, result))

    def _record(self, call: Callable[[RunRecorder], None]) -> None:
        if self.recorder is None:
            return
        try:
            call(self.recorder)
        except Exception:  # recording problems must never break a run
            log.exception("run recorder failed")

    def _line_sink(self, run_id: str, step_id: str) -> Callable[[str, str], None] | None:
        if self.on_log is None and self.recorder is None:
            return None

        def sink(stream: str, text: str) -> None:
            if self._redact is not None:
                text = self._redact(text)
            level, message, fields = parse_line(text)
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            line = LogLine(run_id, step_id, seq, datetime.now(), stream, level, message, fields)
            if self.on_log is not None:
                self.on_log(line)
            self._record(lambda r: r.log(line))

        return sink

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
        result = StepResult(id=cfg.id, type=cfg.type, status="failed", started_at=datetime.now())
        for attempt in range(1, cfg.retries + 2):
            result.attempts = attempt
            start = time.monotonic()
            try:
                outcome = step.execute(ctx, cfg.timeout)
                result.exit_code = outcome.exit_code
                result.stdout, result.stderr = (
                    self._clean(outcome.stdout),
                    self._clean(outcome.stderr),
                )
                if outcome.cancelled:
                    result.status = "cancelled"
                elif outcome.timed_out:
                    result.status = "timeout"
                else:
                    result.status = "success" if outcome.exit_code == 0 else "failed"
            except Exception as exc:  # a plugin bug must not abort the whole run
                result.status, result.exit_code = "failed", None
                result.stderr = self._clean(f"{type(exc).__name__}: {exc}")
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
            if result.status in ("success", "cancelled") or self._cancel.is_set():
                break
            if attempt <= cfg.retries and cfg.retry_delay:
                time.sleep(cfg.retry_delay)
        result.finished_at = datetime.now()
        return result

    def _clean(self, text: str) -> str:
        return self._redact(text) if self._redact is not None else text

    def _expand(self, text: str) -> str:
        if self.secrets is not None:
            return self.secrets.expand(text)
        if REFERENCE.search(text):
            raise SecretError("this run has no secret store, so ${secret:...} cannot be resolved")
        return text

    def _notify(self, run_result: RunResult) -> None:
        for notifier in self._notifiers:
            notifier.expand = self._expand
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
