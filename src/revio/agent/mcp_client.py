"""MCP (Model Context Protocol) client integration.

revio acts as an MCP **client**: at session start, we connect to every MCP
server the user has configured, list each server's tools, and wrap them as
LangChain Tools that the agent can call alongside revio's built-in tools.

Why: enterprises typically already have Jira / Confluence / internal-wiki /
git-platform MCP servers running. Without writing one-off integrations, revio
can use them via the MCP protocol — drop into the user's existing tool
ecosystem.

Lifecycle pattern (anyio task-scope safe):

    async with ClientSessionGroup() as group:
        results = await connect_servers(group, configs)
        tools = langchain_tools_from(group)
        # ... use tools while inside this block ...
    # auto-cleanup

We deliberately do NOT wrap the group in a custom async-context-manager
because anyio's task scopes break when the group's lifecycle is split
across multiple async-context layers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import StdioServerParameters
from mcp.client.session_group import (
    ClientSessionGroup,
    SseServerParameters,
)


logger = logging.getLogger(__name__)


# --- Config schema (config.mcp.servers.<name>) -------------------------------


@dataclass
class MCPServerConfig:
    """One MCP server entry from the user's TOML config."""

    name: str
    # stdio transport (most common, e.g. uvx-launched servers)
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # SSE/HTTP transport
    url: str | None = None
    api_key_env: str | None = None
    # connection timeout
    timeout: float = 5.0

    @property
    def transport(self) -> str:
        if self.url:
            return "sse"
        if self.command:
            return "stdio"
        return "unknown"

    def is_valid(self) -> bool:
        return self.transport in ("stdio", "sse")

    def to_server_params(self):
        """Convert to the SDK's parameter type."""
        if self.transport == "stdio":
            resolved_env = {**os.environ}
            for k, v in self.env.items():
                if isinstance(v, str) and v.startswith("$"):
                    resolved_env[k] = os.environ.get(v[1:], "")
                else:
                    resolved_env[k] = v
            return StdioServerParameters(
                command=self.command or "",
                args=list(self.args),
                env=resolved_env,
            )
        elif self.transport == "sse":
            headers: dict[str, Any] = {}
            if self.api_key_env:
                token = os.environ.get(self.api_key_env, "")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            return SseServerParameters(
                url=self.url or "",
                headers=headers if headers else None,
                timeout=self.timeout,
            )
        else:
            raise ValueError(f"Unknown transport for {self.name!r}")


# --- Connection accounting ---------------------------------------------------


@dataclass
class MCPConnectResult:
    """Per-server result after attempting to connect."""

    name: str
    transport: str
    connected: bool
    new_tool_count: int = 0
    error: str | None = None


# --- Core helpers (called inside `async with ClientSessionGroup()`) ----------


def make_name_hook(per_call_name_holder: dict[str, str]):
    """Build a component-name hook that scopes tool names by current server.

    We use a small dict as a mutable holder so we can swap the current server
    name between connect calls without rebuilding the group.
    """
    def hook(name: str, server_info) -> str:
        sn = per_call_name_holder.get("current") or "mcp"
        return f"mcp_{sn}_{name}"

    return hook


async def connect_servers(
    group: ClientSessionGroup,
    configs: list[MCPServerConfig],
    *,
    name_holder: dict[str, str],
) -> list[MCPConnectResult]:
    """Connect each configured server to `group`. Failures captured, not raised."""
    results: list[MCPConnectResult] = []

    for cfg in configs:
        if not cfg.is_valid():
            results.append(MCPConnectResult(
                name=cfg.name,
                transport="invalid",
                connected=False,
                error="missing both command and url",
            ))
            continue

        before = len(getattr(group, "tools", {})) if hasattr(group, "tools") else 0
        name_holder["current"] = cfg.name

        # Use asyncio.timeout (Python 3.11+) — DO NOT use asyncio.wait_for here.
        # wait_for spawns the coroutine in a new task, which breaks anyio's
        # cancel-scope task-affinity when the outer ClientSessionGroup later
        # tries to clean up the stdio_client's cancel scope.
        try:
            async with asyncio.timeout(cfg.timeout):
                await group.connect_to_server(cfg.to_server_params())
        except asyncio.TimeoutError:
            results.append(MCPConnectResult(
                name=cfg.name, transport=cfg.transport,
                connected=False, error=f"timeout after {cfg.timeout}s",
            ))
            logger.warning("MCP %s: timeout", cfg.name)
            continue
        except Exception as e:
            results.append(MCPConnectResult(
                name=cfg.name, transport=cfg.transport,
                connected=False, error=f"{type(e).__name__}: {e}",
            ))
            logger.warning("MCP %s: connect failed: %s", cfg.name, e)
            continue
        finally:
            name_holder["current"] = ""

        after = len(getattr(group, "tools", {})) if hasattr(group, "tools") else 0
        results.append(MCPConnectResult(
            name=cfg.name, transport=cfg.transport,
            connected=True, new_tool_count=after - before,
        ))
        logger.info("MCP %s: connected, %d new tools", cfg.name, after - before)

    return results


# --- Tool bridging -----------------------------------------------------------


def _extract_text(result: Any) -> str:
    """Flatten MCP CallToolResult content blocks to plain text."""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else "(empty result)"


def _wrap_one(group: ClientSessionGroup, tool_name: str, tool: Any) -> StructuredTool:
    """Wrap one aggregated MCP tool as a LangChain StructuredTool."""
    async def _ainvoke(**kwargs) -> str:
        try:
            result = await group.call_tool(tool_name, arguments=kwargs)
        except Exception as e:
            return f"Error calling MCP tool {tool_name}: {e}"
        if getattr(result, "isError", False):
            return f"MCP tool error: {_extract_text(result)}"
        return _extract_text(result)

    def _sync_invoke(**kwargs) -> str:
        return asyncio.get_event_loop().run_until_complete(_ainvoke(**kwargs))

    description = (tool.description or "") or f"MCP tool {tool_name}"

    return StructuredTool.from_function(
        coroutine=_ainvoke,
        func=_sync_invoke,
        name=tool_name,
        description=f"[mcp] {description}",
        args_schema=getattr(tool, "inputSchema", None) or None,
    )


def langchain_tools_from(group: ClientSessionGroup) -> list[StructuredTool]:
    """Snapshot the group's currently-aggregated tools as LangChain tools."""
    if group is None:
        return []
    tools_dict = getattr(group, "tools", {}) or {}
    out: list[StructuredTool] = []
    for tool_name, tool in tools_dict.items():
        try:
            out.append(_wrap_one(group, tool_name, tool))
        except Exception as e:
            logger.warning("MCP: failed to bridge tool %s: %s", tool_name, e)
    return out


def connection_summary(results: list[MCPConnectResult]) -> str:
    """Human-readable per-server status."""
    if not results:
        return "(no MCP servers configured)"
    lines: list[str] = []
    for r in results:
        if r.connected:
            lines.append(f"  · {r.name} ({r.transport}) → {r.new_tool_count} tools")
        else:
            lines.append(f"  · {r.name} ({r.transport}) → FAILED: {r.error}")
    return "\n".join(lines)
