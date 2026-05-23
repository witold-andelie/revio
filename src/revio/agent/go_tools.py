"""Go profile tools — golangci-lint (Layer 2) + generic AST (Layer 1)."""

from __future__ import annotations

from langchain_core.tools import tool

from .generic_tools import make_generic_ast_tools
from .tool_context import ToolContext


def make_run_golangci_lint_tool(ctx: ToolContext):
    @tool
    def run_golangci_lint(relative_path: str = ".") -> str:
        """Run golangci-lint (composite Go linter: govet + staticcheck + gosec + ...).

        Needs a Go module — there must be a go.mod somewhere in the path
        (or in an ancestor directory).

        Args:
            relative_path: Directory under repo root containing the Go module. Default ".".

        Returns:
            One line per finding: `file:line  [severity]  linter  message`
        """
        runner = ctx.golangci
        if runner is None:
            return (
                "Error: golangci-lint not installed.\n"
                "  brew install golangci-lint   (macOS)\n"
                "  go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest"
            )

        target = (ctx.repo_root / relative_path).resolve()
        try:
            target.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        if not target.is_dir():
            return f"Error: golangci-lint needs a directory: {relative_path}"

        try:
            findings = runner.scan_to_findings(target, repo_root=ctx.repo_root)
        except Exception as e:
            return f"Error running golangci-lint: {e}"

        if not findings:
            return f"(no golangci-lint findings in {relative_path})"

        ctx.pending_findings.extend(findings)

        max_show = 50
        lines = [
            f"golangci-lint findings in {relative_path} ({len(findings)} total — "
            f"auto-recorded, no need to call report_finding for these):"
        ]
        for f in findings[:max_show]:
            linter = f.evidence[0].source if f.evidence else "?"
            lines.append(
                f"  {f.file_path}:{f.line_start}  [{f.severity.value:8}]  "
                f"{linter}  {f.title[:80]}"
            )
        if len(findings) > max_show:
            lines.append(f"  ... ({len(findings) - max_show} more)")
        return "\n".join(lines)

    return run_golangci_lint


def make_go_tools(ctx: ToolContext) -> list:
    tools = list(make_generic_ast_tools(ctx))
    tools.append(make_run_golangci_lint_tool(ctx))
    return tools
