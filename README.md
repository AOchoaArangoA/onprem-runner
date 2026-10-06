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

## Dashboard

```powershell
opr ui                # scans the current directory for pipeline files
opr ui C:utomations --port 9000
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

Windows Task Scheduler integration (`opr schedule`) shown in the dashboard, `opr watch` (file / webhook triggers),
Windows service wrapper, PowerShell / executable step types.

## License

MIT
