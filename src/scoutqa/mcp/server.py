"""Builds the MCP server: `scoutqa mcp` wires one project's config into a stdio server
(docs/ARCHITECTURE.md §3.12). Tool functions in `tools.py` all take a `ToolContext` as their first
parameter; `_bind` produces the actual registered callable with that parameter hidden from the tool's
schema, since the MCP client never sees or passes it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from scoutqa import __version__
from scoutqa.config.models import ProjectConfig
from scoutqa.mcp import tools
from scoutqa.workspace import Workspace, workspace_for

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

# Every tool exposed over MCP, in the order a client would naturally reach for them.
_TOOLS: tuple[Callable[..., Any], ...] = (
    tools.get_project_info, tools.login, tools.crawl_app, tools.get_run_status, tools.get_app_map,
    tools.generate_test_cases, tools.review_cases, tools.export, tools.configure_model, tools.set_template,
    tools.models_list, tools.test_model, tools.get_usage,
)


def _bind(ctx: tools.ToolContext, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Pre-binds `ctx` as `fn`'s first argument. The MCP SDK builds a tool's JSON schema from
    `inspect.signature`, so the wrapper also needs a signature/docstring of its own with `ctx` dropped —
    copying `fn`'s does not happen automatically the way `functools.wraps` would (and `wraps` would keep
    the *original*, ctx-including signature anyway)."""
    wrapper: Callable[..., Any]
    if inspect.iscoroutinefunction(fn):

        async def _awrap(*args: Any, **kwargs: Any) -> Any:
            return await fn(ctx, *args, **kwargs)

        wrapper = _awrap
    else:

        def _wrap(*args: Any, **kwargs: Any) -> Any:
            return fn(ctx, *args, **kwargs)

        wrapper = _wrap

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    sig = inspect.signature(fn)
    wrapper.__signature__ = inspect.Signature(  # type: ignore[union-attr]
        parameters=list(sig.parameters.values())[1:], return_annotation=sig.return_annotation)
    return wrapper


def build_server(cfg: ProjectConfig, ws: Workspace | None = None) -> MCPServer:
    """One server per project (like `scoutqa serve`) — a fixed `scoutqa.yaml`/workspace for the whole
    process, so no tool call can cross project boundaries or needs a project argument of its own."""
    from mcp.server.mcpserver import MCPServer

    ctx = tools.ToolContext(cfg, ws or workspace_for(cfg.project))
    server = MCPServer(
        "scoutqa", version=__version__,
        instructions=(
            f"Crawls {cfg.project} ({cfg.base_url}) and generates manual test cases in your template. "
            "Typical order: get_project_info -> login -> crawl_app (poll get_run_status) -> get_app_map / "
            "generate_test_cases (dry_run=true first to see cost) -> review_cases -> export."
        ),
    )
    for fn in _TOOLS:
        server.add_tool(_bind(ctx, fn), name=fn.__name__, description=(fn.__doc__ or fn.__name__).strip())
    return server


async def run_stdio(cfg: ProjectConfig, ws: Workspace | None = None) -> None:
    server = build_server(cfg, ws)
    await server.run_stdio_async()
