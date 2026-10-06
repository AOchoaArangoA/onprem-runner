from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from onprem_runner.models import RunResult
from onprem_runner.notifiers.base import Notifier
from onprem_runner.notifiers.render import render_csv, render_json, render_text


class FileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    format: Literal["json", "txt", "csv"] = "json"


class FileNotifier(Notifier):
    """Write the run result to a flat file. ``path`` accepts the placeholders
    ``{run_id}``, ``{pipeline}`` and ``{date}`` (YYYYMMDD)."""

    params_model = FileParams
    params: FileParams

    def notify(self, result: RunResult) -> None:
        target = self.base_dir / self.params.path.format(
            run_id=result.run_id,
            pipeline=result.pipeline,
            date=datetime.now().strftime("%Y%m%d"),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        render = {"json": render_json, "txt": render_text, "csv": render_csv}[self.params.format]
        target.write_text(render(result), encoding="utf-8", newline="")
