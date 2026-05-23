"""Java profile tools — spotbugs (Layer 2) + generic AST (Layer 1)."""

from __future__ import annotations

from langchain_core.tools import tool

from .generic_tools import make_generic_ast_tools
from .tool_context import ToolContext


def make_run_spotbugs_tool(ctx: ToolContext):
    @tool
    def run_spotbugs(relative_path: str = ".") -> str:
        """Run SpotBugs (Java bug + security analyzer) on a directory of compiled classes.

        SpotBugs operates on .class files / .jars — NOT raw .java sources.
        If you need to scan raw Java sources, compile them first
        (javac / mvn compile / gradle compileJava).

        Args:
            relative_path: Directory under repo root containing .class files. Default ".".

        Returns:
            One line per finding: `file:line  [severity]  rule_id  message`
        """
        runner = ctx.spotbugs
        if runner is None:
            return (
                "Error: SpotBugs not installed.\n"
                "  brew install spotbugs   (macOS)\n"
                "  Or download from https://spotbugs.github.io/"
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
            return f"Error running spotbugs: {e}"

        if not findings:
            return (
                f"(no spotbugs findings in {relative_path}; note: spotbugs needs "
                f"compiled .class files, not raw .java)"
            )

        # Auto-emit
        ctx.pending_findings.extend(findings)

        max_show = 50
        lines = [
            f"spotbugs findings in {relative_path} ({len(findings)} total — "
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

    return run_spotbugs


def make_java_tools(ctx: ToolContext) -> list:
    tools = list(make_generic_ast_tools(ctx))
    tools.append(make_run_spotbugs_tool(ctx))
    return tools
