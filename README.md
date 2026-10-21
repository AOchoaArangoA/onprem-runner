# onprem-runner

Define, run and monitor automation pipelines on local / on-premise Windows machines:
jobs that cannot live in a cloud scheduler because of OS dependencies, local
credentials, legacy software or an internal network.

```powershell
pip install onprem-runner
opr init            # creates pipeline.yaml and scripts/hello.py
opr validate pipeline.yaml
opr run pipeline.yaml
```

## OPR Studio (desktop app)

A portable Windows app to **design, run and monitor** pipelines visually. Copy
`OPR-Studio-<version>-portable.exe` anywhere and run it: it keeps its database, settings and
pipelines folder next to the `.exe` (or in `%APPDATA%\OPR Studio` if that place is read-only).

- **Canvas**: drag to build the flow, connect steps with *on success / on failure / always* edges,
  run all or one step, watch every node change state live. Parallel branches run at the same time.
- **Logs**: every output line is stored in a local DuckDB database with its time, step and level
  (JSON lines such as `{"level":"warn","msg":"..."}` keep their fields). Search, filter and export.
- **Schedules**: `schedule:` blocks run while the app is open or in the system tray, with pause
  and "run once if a run was missed".
- **Secrets**: stored encrypted for your Windows user; use them as `${secret:NAME}`. They are
  masked in every log.
- **Your files stay yours**: pipelines are plain YAML (comments preserved when edited on the canvas),
  so they work with git and with the `opr` command line too.

Build it yourself, run its tests and read the design in [docs/DESKTOP.md](docs/DESKTOP.md).

## Dashboard

```powershell
opr ui                # scans the current directory for pipeline files
opr ui C:\automations --port 9000
```

A local web UI (no extra dependencies, bound to `127.0.0.1` by default) that shows:

- every pipeline found in the directory, with its steps and live status,
- a **Run** button per pipeline (progress updates step by step),
- the run history with per-step status, duration, `stdout` and `stderr`,
- success rate and runs in the last 24 hours.

Each `opr run` (from the CLI, Task Scheduler or the UI) is recorded in `.opr/runs/` next to the
pipeline file, so scheduled runs also appear in the dashboard. Pass `--no-history` to skip it.
POST requests need a per-session token and the `Host` header is checked, so other web pages
cannot trigger runs. Only expose it with `--host 0.0.0.0` on a network you trust.

## Scheduling (Windows Task Scheduler)

Declare when a pipeline should run, then register it with one command:

```yaml
schedule:
  frequency: daily      # minutes | hourly | daily | weekly | startup
  at: "07:30"           # HH:MM, quoted (daily/weekly)
  # every: 2            # interval: every 2 days / 15 minutes / ...
  # days: [MON, FRI]    # weekly only
```

```powershell
opr schedule install pipeline.yaml            # create or update the task
opr schedule install pipeline.yaml --dry-run  # print the task XML only
opr schedule list                             # tasks with next / last run
opr schedule remove pipeline.yaml
```

Tasks live under the `onprem-runner` folder of Task Scheduler and run
`python -m onprem_runner run <pipeline>` with the interpreter that installed them, as the
current user while logged on. Use `--system` (elevated shell) to run as SYSTEM without a
logged-in user. The dashboard shows each pipeline's schedule and next run, and can
install or remove the task with one click. Runs started by Task Scheduler are recorded in
the dashboard history like any other.

## Control it from Claude Code (MCP)

`opr mcp` exposes your pipelines to an AI agent as an MCP server (stdio):

| Tool | What it does |
|---|---|
| `list_pipelines` | pipelines found, last run, schedule and next run |
| `list_runs` | run history, filter by pipeline / status |
| `get_run` | one run with each step's stdout / stderr (last 4000 chars) |
| `run_pipeline` | start a pipeline; `wait_seconds` waits for the result |
| `set_schedule` | install / remove its Windows scheduled task |

```powershell
pip install "onprem-runner[mcp]"
claude mcp add onprem-runner -- opr mcp C:\automations
```

Use the full path to `opr.exe` if it is not on your `PATH`. Add `--scope project` to share the
setup through a `.mcp.json` in the repository, or `--scope user` for all your projects.

**From claude.ai/code (web or phone).** Cloud sessions run on Anthropic's infrastructure and cannot
start local stdio servers. Instead, keep a session running on the Windows machine and drive it
remotely with Remote Control (Pro/Max/Team/Enterprise, signed in with a claude.ai account):

```powershell
claude remote-control --name "On-prem automation"   # server mode, or
claude --remote-control                             # interactive session
```

The session, the MCP server and every pipeline execute on that machine, so it must stay on and
online. Only the pipelines under the directory you pass to `opr mcp` can be run. Exposing the
server over a public tunnel is not recommended: it has no authentication of its own.

## Pipeline file

```yaml
name: daily-report
fail_fast: true
steps:
  - id: extract
    type: python
    script: scripts/extract.py
    timeout: 600
    retries: 2
    retry_delay: 30
  - id: publish
    type: shell
    command: copy out\report.csv \\server\share
    needs: [extract]
notifiers:
  - type: file
    path: out/{run_id}.json
  - type: email
    when: failure
    smtp_host: smtp.company.local
    username: ${SMTP_USER}
    password: ${SMTP_PASSWORD}
    to: [ops@company.com]
```

- **Steps** run in dependency order (`needs`). A failed or timed-out step makes its
  dependents `skipped`; with `fail_fast: true` (default) every remaining step is skipped.
- **Secrets** are never stored in the YAML: reference environment variables with `${NAME}`.
- **Notifiers** run after the pipeline. `when` is `always` (default), `success` or `failure`.
  A notifier error is logged and never changes the run outcome.

### Built-in types

| Kind | Type | Parameters |
|---|---|---|
| step | `python` | `script`, `args`, `python`, `cwd`, `env` |
| step | `shell` | `command`, `cwd`, `env` |
| notifier | `file` | `path` (`{run_id}`, `{pipeline}`, `{date}`), `format` (`json`/`txt`/`csv`) |
| notifier | `email` | `smtp_host`, `smtp_port`, `security`, `username`, `password`, `from_addr`, `to`, `subject` |

Relative paths resolve against the directory of the pipeline file.

### CLI exit codes

`0` success · `1` the run failed · `2` invalid configuration.
Use `--json-logs` for JSON lines on stderr and `--log-file path` for a JSON log file.

## Extending

Step types and notifiers are plain classes registered through entry points:

```toml
[project.entry-points."onprem_runner.steps"]
powershell = "my_pkg.steps:PowerShellStep"

[project.entry-points."onprem_runner.notifiers"]
teams = "my_pkg.notifiers:TeamsNotifier"
```

See `onprem_runner.steps.StepType` and `onprem_runner.notifiers.Notifier`.

## Roadmap

File/folder, webhook and chained triggers for OPR Studio, a Windows service wrapper,
PowerShell / executable step types, a signed installer and auto-update.

## License

MIT
