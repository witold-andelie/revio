"""End-to-end MCP bridging test with a stub stdio server.

Spawns a minimal MCP server (using the SDK's server-side primitives) that
exposes one tool. Confirms revio:
1. Connects to it via stdio
2. Lists its tools
3. Wraps them as LangChain tools
4. The wrapped tool actually invokes the remote function

Run:
    .venv/bin/python tests/test_mcp_bridge.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from mcp.client.session_group import ClientSessionGroup

from revio.agent.mcp_client import (
    MCPServerConfig,
    connect_servers,
    connection_summary,
    langchain_tools_from,
    make_name_hook,
)


STUB_SERVER_SOURCE = """
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


srv = Server('revio-test-stub')


@srv.list_tools()
async def _list_tools():
    return [
        Tool(
            name='echo',
            description='Return the input string with an [echo] prefix',
            inputSchema={
                'type': 'object',
                'properties': {
                    'text': {'type': 'string', 'description': 'string to echo'},
                },
                'required': ['text'],
            },
        ),
    ]


@srv.call_tool()
async def _call(name, arguments):
    if name == 'echo':
        return [TextContent(type='text', text=f'[echo] {arguments.get(\"text\", \"\")}')]
    return [TextContent(type='text', text=f'unknown tool: {name}')]


async def main():
    async with stdio_server() as (r, w):
        await srv.run(r, w, srv.create_initialization_options())


asyncio.run(main())
"""


async def run_test() -> int:
    f = tempfile.NamedTemporaryFile(mode="w", suffix="_mcp_stub.py", delete=False)
    f.write(STUB_SERVER_SOURCE)
    f.flush()
    f.close()
    stub_path = f.name

    print("=" * 70)
    print("MCP bridge test")
    print("=" * 70)

    cfg = MCPServerConfig(
        name="stub",
        command=sys.executable,
        args=[stub_path],
        timeout=10.0,
    )

    name_holder: dict[str, str] = {"current": ""}
    async with ClientSessionGroup(
        component_name_hook=make_name_hook(name_holder)
    ) as group:
        results = await connect_servers(group, [cfg], name_holder=name_holder)
        print(f"\n{connection_summary(results)}")

        if not results or not results[0].connected:
            print("\n❌ stub server failed to connect")
            return 1

        tools = langchain_tools_from(group)
        print(f"\nBridged tools ({len(tools)}):")
        for t in tools:
            print(f"  · {t.name}  —  {t.description[:80]}")

        if not tools:
            print("\n❌ no tools bridged")
            return 1

        echo_tool = next((t for t in tools if "echo" in t.name), None)
        if echo_tool is None:
            print("\n❌ no echo tool in bridged set")
            return 1

        result = await echo_tool.ainvoke({"text": "hello revio"})
        print(f"\nInvoke result: {result!r}")
        if "[echo] hello revio" not in result:
            print("\n❌ echo result didn't match expected output")
            return 1

    os.unlink(stub_path)
    print("\n✓ ALL MCP BRIDGE CHECKS PASSED")
    return 0


def main() -> int:
    return asyncio.run(run_test())


if __name__ == "__main__":
    raise SystemExit(main())
