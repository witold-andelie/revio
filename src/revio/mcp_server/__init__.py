"""revio's MCP server — exposes revio capabilities to external agents.

When you run `revio mcp-server`, this module starts a stdio MCP server
that lets Claude Code / Cursor / any MCP-aware agent call revio's
review/audit/dedup pipelines and individual analyzers.

Counterpart to `revio.agent.mcp_client` (which lets revio CONSUME other
people's MCP servers). Together: revio is both a client and a server in
the MCP ecosystem.
"""

from .server import build_server, run_stdio

__all__ = ["build_server", "run_stdio"]
