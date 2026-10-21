from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from onprem_runner.errors import ConfigError
from onprem_runner.models import NotifierConfig, RunResult


class Notifier(ABC):
    """Base class for outputs. Subclass and register under the
    ``onprem_runner.notifiers`` entry-point group to add new ones."""

    params_model: ClassVar[type[BaseModel]]

    def __init__(self, config: NotifierConfig, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        # Set by the runner so notifier parameters can use ${secret:NAME}.
        self.expand: Callable[[str], str] = lambda text: text
        try:
            self.params = self.params_model(**config.params)
        except ValidationError as exc:
            raise ConfigError(f"notifier ({config.type}): {exc}") from exc

    def should_notify(self, result: RunResult) -> bool:
        return self.config.when == "always" or self.config.when == (
            "success" if result.status == "success" else "failure"
        )

    @abstractmethod
    def notify(self, result: RunResult) -> None: ...
