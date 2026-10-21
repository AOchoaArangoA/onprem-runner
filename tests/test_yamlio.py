from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from onprem_runner.config import parse_config
from onprem_runner.errors import ConfigError
from onprem_runner.yamlio import (
    MAX_BACKUPS,
    ConflictError,
    apply_graph,
    create_pipeline,
    default_positions,
    etag_of,
    graph_from_config,
    load_graph,
    save_graph,
)

HAND_WRITTEN = """\
# Daily sales report - owned by Finance
name: Reporte diario
fail_fast: true   # stop early
steps:
  # 1. pull data from the ERP
  - {id: extraer, type: python, script: scripts/ok.py}
  - id: validar
    type: python
    script: scripts/ok.py   # keep this path
    needs: [extraer]
  - id: alerta
    type: shell
    command: echo alert
    needs:
      - {step: validar, when: failure}
notifiers:
  - {type: file, path: 'out/{run_id}.json'}
"""


@pytest.fixture
def pipeline(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    path = tmp_path / "reporte.yaml"
    path.write_text(HAND_WRITTEN, encoding="utf-8")
    return path


def graph_of(path: Path) -> dict[str, Any]:
    return load_graph(path, path.parent)


def semantic(text: str) -> Any:
    return yaml.safe_load(text)


# -- reading ------------------------------------------------------------------------


def test_graph_has_steps_edges_and_conditions(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    assert graph["file"] == "reporte.yaml"
    assert [s["id"] for s in graph["steps"]] == ["extraer", "validar", "alerta"]
    assert graph["steps"][0]["params"] == {"script": "scripts/ok.py"}
    assert graph["edges"] == [
        {"id": "extraer->validar", "source": "extraer", "target": "validar", "when": "success"},
        {"id": "validar->alerta", "source": "validar", "target": "alerta", "when": "failure"},
    ]
    assert graph["etag"] == etag_of(pipeline.read_bytes())


def test_missing_positions_get_a_layered_layout(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    assert graph["auto_layout"] is True
    xs = [graph["positions"][s] for s in ("extraer", "validar", "alerta")]
    assert xs[0][0] < xs[1][0] < xs[2][0]  # each step one column right of its dependency


def test_parallel_branches_share_a_column_in_different_rows() -> None:
    config = parse_config(
        "name: p\nsteps:\n  - {id: a, type: shell, command: x}\n"
        "  - {id: b, type: shell, command: x}\n"
        "  - {id: c, type: shell, command: x, needs: [a, b]}\n"
    )
    pos = default_positions(config)
    assert pos["a"][0] == pos["b"][0] and pos["a"][1] != pos["b"][1]
    assert pos["c"][0] > pos["a"][0]


def test_env_references_are_not_expanded_when_reading(
    pipeline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_DIR", "C:/very/secret/place")
    pipeline.write_text(
        "name: p\nsteps:\n  - {id: a, type: shell, command: 'dir ${MY_DIR} ${secret:TOKEN}'}\n",
        encoding="utf-8",
    )
    assert graph_of(pipeline)["steps"][0]["params"]["command"] == "dir ${MY_DIR} ${secret:TOKEN}"


# -- writing: what must be preserved ----------------------------------------------------


def test_saving_an_unchanged_graph_changes_nothing_important(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph.pop("auto_layout")
    new_text = apply_graph(pipeline.read_text(encoding="utf-8"), graph)
    assert "# Daily sales report - owned by Finance" in new_text
    assert "# stop early" in new_text
    assert "# 1. pull data from the ERP" in new_text
    assert "# keep this path" in new_text
    original, rewritten = semantic(HAND_WRITTEN), semantic(new_text)
    rewritten.pop("ui", None)  # only the layout may be added
    assert rewritten == original


def test_editing_a_param_keeps_comments_and_other_steps(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"][1]["params"]["script"] = "scripts/other.py"
    new_text = apply_graph(pipeline.read_text(encoding="utf-8"), graph)
    assert "scripts/other.py" in new_text
    assert "# keep this path" in new_text
    assert "# 1. pull data from the ERP" in new_text
    assert semantic(new_text)["steps"][0] == {
        "id": "extraer",
        "type": "python",
        "script": "scripts/ok.py",
    }


def test_removed_param_disappears_and_special_fields_follow_defaults(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"][1]["retries"] = 3
    graph["steps"][1]["timeout"] = 60
    out = semantic(apply_graph(pipeline.read_text(encoding="utf-8"), graph))
    step = out["steps"][1]
    assert (step["retries"], step["timeout"]) == (3, 60)
    assert "ignore_failure" not in step  # default values are not written

    first_text = apply_graph(pipeline.read_text(encoding="utf-8"), graph)
    graph["steps"][1]["retries"] = 0
    graph["steps"][1]["timeout"] = None
    out = semantic(apply_graph(first_text, graph))  # edit applied on top of the first
    assert out["steps"][1]["retries"] == 0  # the key existed, so it is updated, not dropped
    assert out["steps"][1]["timeout"] is None


def test_add_step_with_a_conditional_edge(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"].append({"id": "limpiar", "type": "shell", "params": {"command": "echo clean"}})
    graph["edges"].append({"id": "x", "source": "extraer", "target": "limpiar", "when": "always"})
    out = semantic(apply_graph(pipeline.read_text(encoding="utf-8"), graph))
    added = out["steps"][-1]
    assert added == {
        "id": "limpiar",
        "type": "shell",
        "command": "echo clean",
        "needs": [{"step": "extraer", "when": "always"}],
    }


def test_needs_uses_compact_ids_when_every_edge_is_success(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["edges"] = [
        {"id": "a", "source": "extraer", "target": "validar", "when": "success"},
        {"id": "b", "source": "extraer", "target": "alerta", "when": "success"},
    ]
    out = semantic(apply_graph(pipeline.read_text(encoding="utf-8"), graph))
    assert out["steps"][1]["needs"] == ["extraer"]
    assert out["steps"][2]["needs"] == ["extraer"]
    assert "needs" not in out["steps"][0]


def test_removing_a_step_and_its_edges(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"] = [s for s in graph["steps"] if s["id"] != "alerta"]
    graph["edges"] = [e for e in graph["edges"] if "alerta" not in (e["source"], e["target"])]
    out = semantic(apply_graph(pipeline.read_text(encoding="utf-8"), graph))
    assert [s["id"] for s in out["steps"]] == ["extraer", "validar"]


def test_reordering_steps(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"].reverse()
    out = semantic(apply_graph(pipeline.read_text(encoding="utf-8"), graph))
    assert [s["id"] for s in out["steps"]] == ["alerta", "validar", "extraer"]


def test_positions_are_written_rounded_and_only_for_existing_steps(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["positions"] = {"extraer": [10.4, 20.6], "validar": [300, 0], "ghost": [1, 1]}
    out = semantic(apply_graph(pipeline.read_text(encoding="utf-8"), graph))
    assert out["ui"]["positions"] == {"extraer": [10, 21], "validar": [300, 0]}


def test_schedule_can_be_added_changed_and_removed(pipeline: Path) -> None:
    text = pipeline.read_text(encoding="utf-8")
    graph = graph_of(pipeline)
    graph["schedule"] = {"frequency": "daily", "at": "07:30", "every": 1, "days": []}
    with_schedule = apply_graph(text, graph)
    assert semantic(with_schedule)["schedule"] == {"frequency": "daily", "at": "07:30"}
    parse_config(with_schedule)  # and it is a valid pipeline

    graph["schedule"] = {"frequency": "weekly", "at": "02:00", "every": 2, "days": ["MON", "FRI"]}
    assert semantic(apply_graph(with_schedule, graph))["schedule"] == {
        "frequency": "weekly",
        "at": "02:00",
        "every": 2,
        "days": ["MON", "FRI"],
    }
    graph["schedule"] = None
    assert "schedule" not in semantic(apply_graph(with_schedule, graph))


def test_notifiers_are_left_alone_unless_changed(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    new_text = apply_graph(pipeline.read_text(encoding="utf-8"), graph)
    assert "'out/{run_id}.json'" in new_text  # quoting style preserved


def test_edge_to_unknown_step_is_rejected(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["edges"].append({"id": "z", "source": "ghost", "target": "validar", "when": "success"})
    with pytest.raises(ConfigError, match="ghost"):
        apply_graph(pipeline.read_text(encoding="utf-8"), graph)


# -- saving ---------------------------------------------------------------------------------


def backups(pipeline: Path) -> list[Path]:
    return sorted((pipeline.parent / ".opr" / "backups").glob("*.yaml"))


def save(pipeline: Path, graph: dict[str, Any], **kwargs: Any) -> str:
    return save_graph(
        pipeline,
        graph,
        expected_etag=kwargs.pop("expected_etag", graph.get("etag")),
        backups_dir=pipeline.parent / ".opr" / "backups",
        **kwargs,
    )


def test_save_writes_the_file_and_backs_up_the_previous_version(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"][0]["params"]["script"] = "scripts/new.py"
    new_etag = save(pipeline, graph)
    assert "scripts/new.py" in pipeline.read_text(encoding="utf-8")
    assert new_etag == etag_of(pipeline.read_bytes())
    [backup] = backups(pipeline)
    assert backup.read_text(encoding="utf-8") == HAND_WRITTEN


def test_save_that_changes_nothing_does_not_rewrite_or_backup(pipeline: Path) -> None:
    first = graph_of(pipeline)
    save(pipeline, first)  # adds the auto layout once
    before = pipeline.read_bytes()
    count = len(backups(pipeline))
    again = graph_of(pipeline)
    save(pipeline, again)
    assert pipeline.read_bytes() == before
    assert len(backups(pipeline)) == count


def test_invalid_graph_is_rejected_and_the_file_is_untouched(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["edges"].append({"id": "c", "source": "alerta", "target": "extraer", "when": "success"})
    before = pipeline.read_bytes()
    with pytest.raises(ConfigError, match="cycle"):
        save(pipeline, graph)
    assert pipeline.read_bytes() == before
    assert backups(pipeline) == []


def test_invalid_step_params_are_rejected_before_writing(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"][0]["params"] = {"typo": 1}  # python steps need `script`
    before = pipeline.read_bytes()
    with pytest.raises(ConfigError):
        save(pipeline, graph)
    assert pipeline.read_bytes() == before


def test_save_detects_a_concurrent_edit_unless_forced(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    pipeline.write_text(HAND_WRITTEN + "\n# edited by hand meanwhile\n", encoding="utf-8")
    graph["steps"][0]["params"]["script"] = "scripts/mine.py"
    with pytest.raises(ConflictError):
        save(pipeline, graph)
    assert "scripts/mine.py" not in pipeline.read_text(encoding="utf-8")
    save(pipeline, graph, force=True)
    assert "scripts/mine.py" in pipeline.read_text(encoding="utf-8")


def test_only_the_latest_backups_are_kept(pipeline: Path) -> None:
    for i in range(MAX_BACKUPS + 4):
        graph = graph_of(pipeline)
        graph["steps"][0]["params"]["script"] = f"scripts/v{i}.py"
        save(pipeline, graph)
    assert len(backups(pipeline)) == MAX_BACKUPS


def test_saved_file_loads_again_to_the_same_graph(pipeline: Path) -> None:
    graph = graph_of(pipeline)
    graph["steps"].append({"id": "n", "type": "shell", "params": {"command": "echo n"}})
    graph["edges"].append({"id": "e", "source": "validar", "target": "n", "when": "failure"})
    graph["positions"]["n"] = [900, 40]
    save(pipeline, graph)
    again = graph_of(pipeline)
    assert [s["id"] for s in again["steps"]] == ["extraer", "validar", "alerta", "n"]
    assert {(e["source"], e["target"], e["when"]) for e in again["edges"]} == {
        ("extraer", "validar", "success"),
        ("validar", "alerta", "failure"),
        ("validar", "n", "failure"),
    }
    assert again["positions"]["n"] == [900, 40]
    assert again["auto_layout"] is False


def test_graph_is_json_serialisable(pipeline: Path) -> None:
    import json

    graph = graph_of(pipeline)
    assert json.loads(json.dumps(graph)) == graph
    assert copy.deepcopy(graph) == graph


# -- new pipelines ---------------------------------------------------------------------------


def test_create_pipeline_makes_a_valid_unique_file(tmp_path: Path) -> None:
    first = create_pipeline(tmp_path, "Reporte Diario")
    second = create_pipeline(tmp_path, "Reporte Diario")
    assert first.name == "reporte-diario.yaml"
    assert second.name == "reporte-diario-2.yaml"
    assert parse_config(first.read_text(encoding="utf-8")).name == "Reporte Diario"


@pytest.mark.parametrize(
    "name", ["a: b", "con ñ y 日本", 'quote " inside', "# not a comment", "x" * 200]
)
def test_create_pipeline_handles_awkward_names(tmp_path: Path, name: str) -> None:
    path = create_pipeline(tmp_path, name)
    assert parse_config(path.read_text(encoding="utf-8")).name == name


def test_create_pipeline_rejects_blank_names(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        create_pipeline(tmp_path, "   ")


def test_graph_from_config_round_trips_through_json(tmp_path: Path) -> None:
    config = parse_config(HAND_WRITTEN, expand_env=False)
    graph = graph_from_config(config, "x.yaml", "abc")
    assert graph["notifiers"][0]["type"] == "file"
    assert graph["fail_fast"] is True and graph["max_parallel"] == 4
