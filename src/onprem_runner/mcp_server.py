"""MCP server (stdio) so an AI agent such as Claude Code can drive local pipelines.

Requires the optional dependency: ``pip install "onprem-runner[mcp]"``.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from onprem_runner.control import ControlError, PipelineControl
from onprem_runner.scheduler import SchedulerBackend

INSTRUCTIONS = (
    "Controls automation pipelines that run on this on-premise Windows machine. "
    "Call list_pipelines first to get the `file` identifiers the other tools expect. "
    "run_pipeline executes real processes on the machine; set_schedule changes Windows "
    "Task Scheduler. Confirm with the user before using either on a pipeline they have not named."
)


def _load_server_class() -> Any:
    # mcp 2.x renamed FastMCP to MCPServer; support both. importlib keeps this
    # type-checkable regardless of which major version is installed.
    for module, attr in (("mcp.server.mcpserver", "MCPServer"), ("mcp.server.fastmcp", "FastMCP")):
        try:
            return getattr(importlib.import_module(module), attr)
        except (ImportError, AttributeError):
            continue
    raise ControlError(
        'the MCP server needs the optional dependency: pip install "onprem-runner[mcp]"'
    )


def _tool_error_class() -> type[Exception]:
    """ToolError is how an MCP tool reports a failure with a readable message."""
    for module in ("mcp.server.mcpserver.exceptions", "mcp.server.fastmcp.exceptions"):
        try:
            return importlib.import_module(module).ToolError  # type: ignore[no-any-return]
        except (ImportError, AttributeError):
            continue
    return ValueError


def create_mcp_server(root: Path, scheduler: SchedulerBackend | None = None) -> Any:
    from mcp.types import ToolAnnotations

    control = PipelineControl(root, scheduler)
    tool_error = _tool_error_class()
    server = _load_server_class()("onprem-runner", instructions=INSTRUCTIONS)

    read_only = ToolAnnotations(readOnlyHint=True)
    acts = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    def guarded(fn: Any) -> Any:
        """Turn ControlError into a message the agent can read instead of a stack trace."""

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except ControlError as exc:
                raise tool_error(str(exc)) from exc

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]  # keep the real signature visible
        return wrapper

    server.tool(annotations=read_only)(guarded(control.list_pipelines))
    server.tool(annotations=read_only)(guarded(control.list_runs))
    server.tool(annotations=read_only)(guarded(control.get_run))
    server.tool(annotations=acts)(guarded(control.run_pipeline))
    server.tool(annotations=acts)(guarded(control.set_schedule))
    return server


def serve(root: Path) -> None:
    create_mcp_server(root).run()  # stdio transport; never print to stdout here
