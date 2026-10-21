"""Read and write pipeline YAML as a graph, for the visual editor.

Writing is done in place with ruamel.yaml's round-trip mode so comments, key order and quoting
that a user wrote by hand survive an edit made on the canvas. Every save is validated first,
backed up, and written atomically.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from onprem_runner.config import execution_order, parse_config
from onprem_runner.errors import ConfigError
from onprem_runner.models import PipelineConfig

# Step fields the editor manages; anything else in a step is a parameter of its type.
STEP_DEFAULTS: dict[str, Any] = {
    "timeout": None,
    "retries": 0,
    "retry_delay": 0.0,
    "ignore_failure": False,
}
TOP_DEFAULTS: dict[str, Any] = {"fail_fast": True, "max_parallel": 4}
MANAGED_STEP_KEYS = {"id", "type", "needs", *STEP_DEFAULTS}
LAYER_WIDTH = 300
ROW_HEIGHT = 130
MAX_BACKUPS = 10


class ConflictError(Exception):
    """The file changed on disk after the editor loaded it."""


def etag_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _yaml() -> YAML:
    yaml = YAML()  # round-trip
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _plain(value: Any) -> Any:
    """ruamel containers/scalars to plain JSON-compatible Python."""
    return json.loads(json.dumps(value, default=str))


# -- graph from a config ---------------------------------------------------------------


def default_positions(config: PipelineConfig) -> dict[str, list[float]]:
    """Layered layout: a step sits one column right of its deepest dependency."""
    depth: dict[str, int] = {}
    for step in execution_order(config.steps):
        depth[step.id] = 1 + max((depth[d] for d in step.needs_ids), default=-1)
    rows: dict[int, int] = {}
    positions: dict[str, list[float]] = {}
    for step in config.steps:
        column = depth[step.id]
        row = rows.get(column, 0)
        rows[column] = row + 1
        positions[step.id] = [column * LAYER_WIDTH, row * ROW_HEIGHT]
    return positions


def graph_from_config(
    config: PipelineConfig,
    file: str,
    etag: str,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``raw`` is the file as written; notifiers are taken from it so defaults the
    user never typed are not echoed back (and later written) by the editor."""
    positions = dict(config.ui.positions) if config.ui else {}
    missing = [s.id for s in config.steps if s.id not in positions]
    if missing:
        laid_out = default_positions(config)
        for step_id in missing:
            positions[step_id] = laid_out[step_id]
    return {
        "file": file,
        "etag": etag,
        "name": config.name,
        "fail_fast": config.fail_fast,
        "max_parallel": config.max_parallel,
        "steps": [
            {
                "id": s.id,
                "type": s.type,
                "params": _plain(s.params),
                "timeout": s.timeout,
                "retries": s.retries,
                "retry_delay": s.retry_delay,
                "ignore_failure": s.ignore_failure,
            }
            for s in config.steps
        ],
        "edges": [
            {"id": f"{edge.step}->{s.id}", "source": edge.step, "target": s.id, "when": edge.when}
            for s in config.steps
            for edge in s.needs
        ],
        "positions": positions,
        "auto_layout": bool(missing),
        "schedule": config.schedule.model_dump() if config.schedule else None,
        "notifiers": (
            _plain(raw.get("notifiers") or [])
            if raw is not None
            else [n.model_dump() for n in config.notifiers]
        ),
    }


def load_graph(path: Path, root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = data.decode("utf-8")
    config = parse_config(text, source=str(path), expand_env=False)
    raw = pyyaml.safe_load(text)
    return graph_from_config(config, path.relative_to(root).as_posix(), etag_of(data), raw)


# -- graph to YAML text ----------------------------------------------------------------


def _flow(items: list[Any]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _needs_value(edges: list[dict[str, Any]]) -> list[Any]:
    entries: list[Any] = []
    for edge in edges:
        if edge.get("when", "success") == "success":
            entries.append(edge["source"])
        else:
            entry = CommentedMap([("step", edge["source"]), ("when", edge["when"])])
            entry.fa.set_flow_style()
            entries.append(entry)
    return entries


def _sync_step(existing: CommentedMap, step: dict[str, Any], edges: list[dict[str, Any]]) -> None:
    desired: dict[str, Any] = {"id": step["id"], "type": step["type"]}
    desired.update(step.get("params") or {})
    for key, default in STEP_DEFAULTS.items():
        value = step.get(key, default)
        if value != default or key in existing:
            desired[key] = value
    if edges:
        desired["needs"] = _needs_value(edges)

    for key in list(existing.keys()):
        if key not in desired:
            del existing[key]
    for key, value in desired.items():
        if key not in existing or _plain(existing[key]) != _plain(value):
            existing[key] = value


def apply_graph(text: str, graph: dict[str, Any]) -> str:
    """Return ``text`` updated to match ``graph``, preserving what the editor does not touch."""
    yaml = _yaml()
    doc = yaml.load(text)
    if not isinstance(doc, CommentedMap):
        doc = CommentedMap()

    doc["name"] = graph["name"]
    for key, default in TOP_DEFAULTS.items():
        if graph.get(key, default) != default or key in doc:
            doc[key] = graph.get(key, default)

    existing_steps = {
        _plain(s.get("id")): s for s in doc.get("steps", []) if isinstance(s, CommentedMap)
    }
    incoming = {e["target"] for e in graph.get("edges", [])}
    ids = [s["id"] for s in graph["steps"]]
    unknown = (incoming | {e["source"] for e in graph.get("edges", [])}) - set(ids)
    if unknown:
        raise ConfigError(f"edges refer to unknown steps: {sorted(unknown)}")

    rebuilt = CommentedSeq()
    for step in graph["steps"]:
        target = existing_steps.get(step["id"]) or CommentedMap()
        edges = [e for e in graph.get("edges", []) if e["target"] == step["id"]]
        _sync_step(target, step, edges)
        rebuilt.append(target)
    steps_seq = doc.get("steps")
    if isinstance(steps_seq, CommentedSeq):
        steps_seq.clear()
        steps_seq.extend(rebuilt)
    else:
        doc["steps"] = rebuilt

    if "schedule" in graph:
        schedule = graph["schedule"]
        if schedule is None:
            doc.pop("schedule", None)
        else:
            wanted = {k: v for k, v in schedule.items() if _keep_schedule(k, v)}
            if _plain(doc.get("schedule")) != wanted:  # untouched blocks keep their comments
                block = CommentedMap(wanted)
                block.fa.set_flow_style()
                doc["schedule"] = block
    if "notifiers" in graph and _plain(doc.get("notifiers", [])) != graph["notifiers"]:
        if graph["notifiers"]:
            doc["notifiers"] = graph["notifiers"]
        else:
            doc.pop("notifiers", None)

    positions = graph.get("positions") or {}
    if positions:
        ui = doc.get("ui")
        if not isinstance(ui, CommentedMap):
            ui = CommentedMap()
            doc["ui"] = ui
        block = CommentedMap()
        for step_id in ids:
            if step_id in positions:
                x, y = positions[step_id]
                block[step_id] = _flow([round(x), round(y)])
        ui["positions"] = block

    out = io.StringIO()
    yaml.dump(doc, out)
    return out.getvalue()


def _keep_schedule(key: str, value: Any) -> bool:
    """Drop optional schedule fields that hold their default (``days: []``, ``every: 1``...)."""
    if value is None or value == []:
        return False
    return not ((key == "every" and value == 1) or (key == "catch_up" and value is True))


# -- validation and saving ---------------------------------------------------------------


def validate_text(text: str, base_dir: Path) -> PipelineConfig:
    """Everything that can be checked without running: schema, graph, step/notifier params."""
    from onprem_runner.runner import Runner  # local import: runner pulls in many modules

    config = parse_config(text, expand_env=False)
    Runner(config, base_dir)
    return config


def _prune_backups(directory: Path, stem: str) -> None:
    backups = sorted(directory.glob(f"{stem}.*.yaml"), reverse=True)
    for old in backups[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_graph(
    path: Path,
    graph: dict[str, Any],
    *,
    expected_etag: str | None,
    backups_dir: Path,
    force: bool = False,
) -> str:
    """Validate and save; returns the new etag. The file is untouched if anything fails."""
    current = path.read_bytes()
    if expected_etag and not force and etag_of(current) != expected_etag:
        raise ConflictError(f"{path.name} changed on disk since it was opened")
    new_text = apply_graph(current.decode("utf-8"), graph)
    validate_text(new_text, path.parent)

    if new_text.encode("utf-8") == current:
        return etag_of(current)  # nothing changed: no rewrite, no backup noise
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (backups_dir / f"{path.stem}.{stamp}.yaml").write_bytes(current)
    _prune_backups(backups_dir, path.stem)
    write_atomic(path, new_text)
    return etag_of(new_text.encode("utf-8"))


# -- new pipelines ---------------------------------------------------------------------


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:60] or "pipeline"


def new_pipeline_text(name: str) -> str:
    quoted = json.dumps(name, ensure_ascii=False)
    return (
        f"name: {quoted}\n"
        "steps:\n"
        "  - id: hello\n"
        "    type: shell\n"
        "    command: echo Hello from OPR Studio\n"
        "ui:\n"
        "  positions:\n"
        "    hello: [0, 0]\n"
    )


def create_pipeline(directory: Path, name: str) -> Path:
    if not name.strip():
        raise ConfigError("a pipeline needs a name")
    base = slugify(name)
    candidate = directory / f"{base}.yaml"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base}-{counter}.yaml"
        counter += 1
    text = new_pipeline_text(name.strip())
    parse_config(text, expand_env=False)  # a name like "a: b" must still produce valid YAML
    directory.mkdir(parents=True, exist_ok=True)
    candidate.write_text(text, encoding="utf-8", newline="\n")
    return candidate
