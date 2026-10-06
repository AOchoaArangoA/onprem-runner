from __future__ import annotations

from importlib.metadata import entry_points
from typing import TypeVar

from onprem_runner.errors import ConfigError
from onprem_runner.notifiers import EmailNotifier, FileNotifier, Notifier
from onprem_runner.steps import PythonStep, ShellStep, StepType

STEP_GROUP = "onprem_runner.steps"
NOTIFIER_GROUP = "onprem_runner.notifiers"

_BUILTIN_STEPS: dict[str, type[StepType]] = {"python": PythonStep, "shell": ShellStep}
_BUILTIN_NOTIFIERS: dict[str, type[Notifier]] = {"file": FileNotifier, "email": EmailNotifier}


T = TypeVar("T")


def _plugins(group: str, builtin: dict[str, type[T]]) -> dict[str, type[T]]:
    found = dict(builtin)
    for ep in entry_points(group=group):
        found[ep.name] = ep.load()
    return found


def get_step_type(name: str) -> type[StepType]:
    steps = _plugins(STEP_GROUP, _BUILTIN_STEPS)
    if name not in steps:
        raise ConfigError(f"unknown step type {name!r}; available: {', '.join(sorted(steps))}")
    return steps[name]


def get_notifier_type(name: str) -> type[Notifier]:
    notifiers = _plugins(NOTIFIER_GROUP, _BUILTIN_NOTIFIERS)
    if name not in notifiers:
        raise ConfigError(
            f"unknown notifier type {name!r}; available: {', '.join(sorted(notifiers))}"
        )
    return notifiers[name]
