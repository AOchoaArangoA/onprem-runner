from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from onprem_runner import __version__
from onprem_runner.config import execution_order, load_config
from onprem_runner.errors import ConfigError
from onprem_runner.logging_utils import setup_logging
from onprem_runner.runner import Runner
from onprem_runner.scheduler import SchedulerError, TaskScheduler, build_task_xml
from onprem_runner.store import RunStore

app = typer.Typer(help="Run local/on-premise automation pipelines.", no_args_is_help=True)

schedule_app = typer.Typer(
    help="Manage Windows Task Scheduler tasks for pipelines.", no_args_is_help=True
)
app.add_typer(schedule_app, name="schedule")

PipelineArg = Annotated[Path, typer.Argument(help="Path to the pipeline YAML file.")]

SAMPLE_PIPELINE = """\
name: hello-pipeline
fail_fast: true
steps:
  - id: hello
    type: python
    script: scripts/hello.py
    timeout: 60
  - id: echo
    type: shell
    command: echo done
    needs: [hello]
notifiers:
  - type: file
    path: out/{run_id}.json
    format: json
  # - type: email
  #   when: failure
  #   smtp_host: smtp.example.com
  #   username: ${SMTP_USER}
  #   password: ${SMTP_PASSWORD}
  #   to: [ops@example.com]
"""

SAMPLE_SCRIPT = 'print("Hello from onprem-runner!")\n'


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(2)


@app.command()
def run(
    pipeline: PipelineArg,
    json_logs: Annotated[
        bool, typer.Option("--json-logs", help="Emit logs as JSON lines.")
    ] = False,
    log_file: Annotated[
        Path | None, typer.Option(help="Also write JSON logs to this file.")
    ] = None,
    no_history: Annotated[
        bool, typer.Option("--no-history", help="Do not record this run for the dashboard.")
    ] = False,
) -> None:
    """Run a pipeline now. Exit code: 0 success, 1 run failed, 2 invalid config."""
    setup_logging(json_format=json_logs, log_file=log_file)
    try:
        config = load_config(pipeline)
        base_dir = pipeline.resolve().parent
        runner = Runner(
            config,
            base_dir,
            store=None if no_history else RunStore(base_dir),
            pipeline_file=pipeline.resolve(),
        )
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    result = runner.run()
    raise typer.Exit(0 if result.status == "success" else 1)


@app.command()
def validate(pipeline: PipelineArg) -> None:
    """Check a pipeline file without running it (unset ${ENV} references are allowed)."""
    try:
        config = load_config(pipeline, strict_env=False)
        Runner(config, pipeline.resolve().parent)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    order = " -> ".join(s.id for s in execution_order(config.steps))
    typer.secho(f"OK: {config.name} ({order})", fg=typer.colors.GREEN)


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Target directory.")] = Path(),
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Create a sample pipeline.yaml and scripts/hello.py."""
    files = {
        directory / "pipeline.yaml": SAMPLE_PIPELINE,
        directory / "scripts" / "hello.py": SAMPLE_SCRIPT,
    }
    existing = [p for p in files if p.exists()]
    if existing and not force:
        raise _fail(f"{existing[0]} already exists (use --force to overwrite)")
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        typer.echo(f"created {path}")


@app.command()
def ui(
    directory: Annotated[
        Path, typer.Argument(help="Directory to scan for pipeline files.")
    ] = Path(),
    host: Annotated[
        str, typer.Option(help="Interface to bind. Keep 127.0.0.1 unless you trust the network.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the browser.")
    ] = True,
) -> None:
    """Start the local web dashboard (pipelines, live runs and history)."""
    from onprem_runner.ui import create_server

    setup_logging()
    if not directory.is_dir():
        raise _fail(f"{directory} is not a directory")
    try:
        server = create_server(directory, host, port)
    except OSError as exc:
        raise _fail(f"cannot listen on {host}:{port}: {exc}") from exc
    url = f"http://{host}:{server.server_address[1]}/"
    typer.secho(f"Dashboard: {url}  (Ctrl+C to stop)", fg=typer.colors.GREEN)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("bye")
    finally:
        server.server_close()


@schedule_app.command("install")
def schedule_install(
    pipeline: PipelineArg,
    system: Annotated[
        bool,
        typer.Option(
            "--system",
            help="Run as SYSTEM, even when nobody is logged on (needs an elevated shell).",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the task XML, change nothing.")
    ] = False,
) -> None:
    """Register (or update) the pipeline's `schedule:` block as a Windows scheduled task."""
    try:
        config = load_config(pipeline, strict_env=False)
        Runner(config, pipeline.resolve().parent)
        if config.schedule is None:
            raise ConfigError(f"{pipeline} has no `schedule:` block")
        if dry_run:
            typer.echo(build_task_xml(config.schedule, pipeline, system=system))
            return
        name = TaskScheduler().install(pipeline, config.name, config.schedule, system=system)
    except (ConfigError, SchedulerError) as exc:
        raise _fail(str(exc)) from exc
    typer.secho(f"Installed {name}: runs {config.schedule.describe()}", fg=typer.colors.GREEN)


@schedule_app.command("remove")
def schedule_remove(pipeline: PipelineArg) -> None:
    """Delete the scheduled task of a pipeline."""
    try:
        config = load_config(pipeline, strict_env=False)
        removed = TaskScheduler().remove(pipeline, config.name)
    except (ConfigError, SchedulerError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo("Removed." if removed else "No scheduled task found for this pipeline.")


@schedule_app.command("list")
def schedule_list() -> None:
    """List every task registered by onprem-runner with its next and last run."""
    try:
        tasks = TaskScheduler().list_tasks()
    except SchedulerError as exc:
        raise _fail(str(exc)) from exc
    if not tasks:
        typer.echo("No scheduled tasks.")
    for task in tasks.values():
        typer.echo(
            f"{task.name}  [{task.state}]  next: {task.next_run or '-'}  "
            f"last: {task.last_run or '-'} (result {task.last_result})"
        )


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)
