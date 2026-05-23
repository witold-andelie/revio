"""Python profile tools — bandit (Layer 2) + generic AST (Layer 1)."""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool

from .generic_tools import make_generic_ast_tools
from .tool_context import ToolContext


def make_run_bandit_tool(ctx: ToolContext):
    @tool
    def run_bandit(relative_path: str = ".") -> str:
        """Run bandit (Python security linter) on a file or directory.

        Surfaces hardcoded passwords, pickle deserialization, shell=True,
        weak crypto, eval, assert in prod, etc.

        Args:
            relative_path: File or directory under repo root. Default "." for whole repo.

        Returns:
            One line per finding: `file:line  [severity]  test_id  message`
        """
        runner = ctx.bandit
        if runner is None:
            return "Error: bandit not installed. Install with: pip install bandit"

        target = (ctx.repo_root / relative_path).resolve()
        try:
            target.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        if not target.exists():
            return f"Error: not found: {relative_path}"

        try:
            findings = runner.scan_to_findings(target, repo_root=ctx.repo_root)
        except Exception as e:
            return f"Error running bandit: {e}"

        if not findings:
            return f"(no bandit issues in {relative_path})"

        # Auto-emit (no LLM re-emit needed)
        ctx.pending_findings.extend(findings)

        max_show = 50
        lines = [
            f"bandit findings in {relative_path} ({len(findings)} total — "
            f"auto-recorded, no need to call report_finding for these):"
        ]
        for f in findings[:max_show]:
            test_id = f.evidence[0].source if f.evidence else "?"
            lines.append(
                f"  {f.file_path}:{f.line_start}  [{f.severity.value:8}]  "
                f"{test_id}  {f.title[:80]}"
            )
        if len(findings) > max_show:
            lines.append(f"  ... ({len(findings) - max_show} more)")
        return "\n".join(lines)

    return run_bandit


def make_python_tools(ctx: ToolContext) -> list:
    """Python profile's tool bundle."""
    tools = list(make_generic_ast_tools(ctx))
    tools.append(make_run_bandit_tool(ctx))
    return tools
