"""Smoke test for revio's MCP server.

Launches `revio mcp-server` as a stdio subprocess via the official MCP
SDK client and exercises the tools that DON'T need an LLM call:

  · list_tools           — enumerate the surface
  · revio_list_profiles  — discovery (instant)
  · revio_detect_profile — fingerprint the revio repo itself
  · revio_run_bandit     — Layer-2-only scan (skipped if bandit missing)

We deliberately do NOT exercise revio_audit / revio_review / revio_dedup
here — those need an LLM, so they live in M3's real-API runs instead.

Run:
    .venv/bin/python tests/test_mcp_server_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REPO_ROOT = Path(__file__).resolve().parent.parent


def _server_params() -> StdioServerParameters:
    """Launch the revio CLI's mcp-server subcommand via the local venv."""
    py = REPO_ROOT / ".venv" / "bin" / "python"
    return StdioServerParameters(
        command=str(py),
        args=["-m", "revio.cli.main", "mcp-server"],
        env=None,
    )


async def main() -> int:
    print("Spawning revio mcp-server subprocess...")
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("  · session initialized")

            # 1) Enumerate tools
            tools_response = await session.list_tools()
            tool_names = [t.name for t in tools_response.tools]
            print(f"  · {len(tool_names)} tools exposed: {', '.join(tool_names[:5])}...")
            assert "revio_audit" in tool_names, "missing revio_audit"
            assert "revio_dedup" in tool_names, "missing revio_dedup"
            assert "revio_list_profiles" in tool_names, "missing revio_list_profiles"
            assert "revio_detect_profile" in tool_names, "missing revio_detect_profile"

            # 2) revio_list_profiles
            result = await session.call_tool("revio_list_profiles", arguments={})
            assert not result.isError, f"list_profiles errored: {result}"
            text = result.content[0].text if result.content else ""
            profiles = json.loads(text)
            assert isinstance(profiles, list) and len(profiles) > 0, "no profiles returned"
            profile_names = [p["name"] for p in profiles]
            print(f"  · revio_list_profiles → {len(profiles)} profiles ({', '.join(profile_names[:6])}...)")
            assert "python" in profile_names
            assert "js" in profile_names

            # 3) revio_detect_profile against the revio repo itself
            result = await session.call_tool(
                "revio_detect_profile",
                arguments={"repo_path": str(REPO_ROOT)},
            )
            assert not result.isError, f"detect_profile errored: {result}"
            text = result.content[0].text if result.content else ""
            fingerprint = json.loads(text)
            assert "suggested_profile" in fingerprint, f"no suggested_profile in {fingerprint}"
            print(f"  · revio_detect_profile(self) → suggested={fingerprint['suggested_profile']}")

            # 4) revio_run_bandit on a tiny Python sample (skip if bandit not installed)
            sample = REPO_ROOT / "tests" / "fixtures" / "multilang" / "python_sample"
            if sample.exists():
                result = await session.call_tool(
                    "revio_run_bandit",
                    arguments={"path": str(sample)},
                )
                text = result.content[0].text if result.content else ""
                payload = json.loads(text)
                if "error" in payload and "not installed" in payload.get("error", "").lower():
                    print("  · revio_run_bandit → bandit not installed (skipped)")
                else:
                    assert payload.get("analyzer") == "bandit"
                    assert "findings" in payload
                    print(f"  · revio_run_bandit({sample.name}) → {payload['count']} findings")
            else:
                print(f"  · revio_run_bandit → sample missing, skipped")

    print("\nAll MCP server smoke checks PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
