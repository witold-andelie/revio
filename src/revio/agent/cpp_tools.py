"""C/C++ profile tools — cppcheck (Layer 2) + generic AST (Layer 1)."""

from __future__ import annotations

from langchain_core.tools import tool

from .generic_tools import make_generic_ast_tools
from .tool_context import ToolContext


def make_run_cppcheck_tool(ctx: ToolContext):
    @tool
    def run_cppcheck(relative_path: str = ".") -> str:
        """Run cppcheck (C/C++ static analyzer) on a file or directory.

        Catches buffer overflows, null derefs, uninitialized variables,
        memory leaks, integer overflows, etc.

        Args:
            relative_path: File or directory under repo root. Default ".".

        Returns:
            One line per finding: `file:line  [severity]  rule_id  message`
        """
        runner = ctx.cppcheck
        if runner is None:
            return (
                "Error: cppcheck not installed.\n"
                "  brew install cppcheck    (macOS)\n"
                "  apt install cppcheck     (Debian/Ubuntu)"
            )

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
            return f"Error running cppcheck: {e}"

        if not findings:
            return f"(no cppcheck findings in {relative_path})"

        ctx.pending_findings.extend(findings)

        max_show = 50
        lines = [
            f"cppcheck findings in {relative_path} ({len(findings)} total — "
            f"auto-recorded, no need to call report_finding for these):"
        ]
        for f in findings[:max_show]:
            rule = f.evidence[0].source if f.evidence else "?"
            lines.append(
                f"  {f.file_path}:{f.line_start}  [{f.severity.value:8}]  "
                f"{rule}  {f.title[:80]}"
            )
        if len(findings) > max_show:
            lines.append(f"  ... ({len(findings) - max_show} more)")
        return "\n".join(lines)

    return run_cppcheck


def make_cpp_tools(ctx: ToolContext) -> list:
    tools = list(make_generic_ast_tools(ctx))
    tools.append(make_run_cppcheck_tool(ctx))
    return tools
