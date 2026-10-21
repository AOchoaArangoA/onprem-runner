from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from onprem_runner.errors import ConfigError
from onprem_runner.models import PipelineConfig, StepConfig

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, strict: bool) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            resolved = os.environ.get(match.group(1))
            if resolved is None:
                if strict:
                    raise ConfigError(f"environment variable {match.group(1)!r} is not set")
                return match.group(0)
            return resolved

        return _ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(v, strict) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v, strict) for k, v in value.items()}
    return value


def load_config(path: Path, *, strict_env: bool = True) -> PipelineConfig:
    """Load and validate a pipeline YAML, expanding ``${VAR}`` references."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        config = PipelineConfig.model_validate(_expand_env(raw, strict_env))
    except ValidationError as exc:
        raise ConfigError(f"invalid pipeline {path}:\n{exc}") from exc
    execution_order(config.steps)  # validates ids and dependency graph
    return config


def execution_order(steps: list[StepConfig]) -> list[StepConfig]:
    """Topological order (Kahn), stable with respect to declaration order."""
    by_id: dict[str, StepConfig] = {}
    for step in steps:
        if step.id in by_id:
            raise ConfigError(f"duplicate step id {step.id!r}")
        by_id[step.id] = step
    for step in steps:
        seen: set[str] = set()
        for dep in step.needs_ids:
            if dep not in by_id:
                raise ConfigError(f"step {step.id!r} needs unknown step {dep!r}")
            if dep == step.id:
                raise ConfigError(f"step {step.id!r} cannot depend on itself")
            if dep in seen:
                raise ConfigError(f"step {step.id!r} lists {dep!r} more than once in needs")
            seen.add(dep)

    ordered: list[StepConfig] = []
    done: set[str] = set()
    pending = list(steps)
    while pending:
        ready = next((s for s in pending if all(d in done for d in s.needs_ids)), None)
        if ready is None:
            cycle = ", ".join(s.id for s in pending)
            raise ConfigError(f"dependency cycle among steps: {cycle}")
        pending.remove(ready)
        ordered.append(ready)
        done.add(ready.id)
    return ordered
