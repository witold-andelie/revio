"""Rust profile tools — clippy (Layer 2) + generic AST (Layer 1)."""

from __future__ import annotations

from langchain_core.tools import tool

from .generic_tools import make_generic_ast_tools
from .tool_context import ToolContext


def make_run_clippy_tool(ctx: ToolContext):
    @tool
    def run_clippy(relative_path: str = ".") -> str:
        """Run cargo clippy (Rust linter) on a Cargo project directory.

        Surfaces 600+ Rust lints: correctness, complexity, performance, style,
        common anti-patterns. Requires the target to contain a Cargo.toml.

        Args:
            relative_path: Cargo project directory under repo root. Default ".".

        Returns:
            One line per finding: `file:line  [severity]  lint_name  message`
        """
        runner = ctx.clippy
        if runner is None:
            return (
                "Error: cargo clippy not installed. Install rustup + clippy:\n"
                "  curl https://sh.rustup.rs -sSf | sh\n"
                "  rustup component add clippy"
            )

        target = (ctx.repo_root / relative_path).resolve()
        try:
            target.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        if not target.is_dir():
            return f"Error: clippy needs a directory (Cargo project root): {relative_path}"

        if not (target / "Cargo.toml").is_file():
            return f"Error: no Cargo.toml at {relative_path} — clippy needs a Cargo project"

        try:
            findings = runner.scan_to_findings(target, repo_root=ctx.repo_root)
        except Exception as e:
            return f"Error running clippy: {e}"

        if not findings:
            return f"(no clippy lints in {relative_path})"

        # Auto-emit
        ctx.pending_findings.extend(findings)

        max_show = 50
        lines = [
            f"clippy findings in {relative_path} ({len(findings)} total — "
            f"auto-recorded, no need to call report_finding for these):"
        ]
        for f in findings[:max_show]:
            lint = f.evidence[0].source if f.evidence else "?"
            lines.append(
                f"  {f.file_path}:{f.line_start}  [{f.severity.value:8}]  "
                f"{lint}  {f.title[:80]}"
            )
        if len(findings) > max_show:
            lines.append(f"  ... ({len(findings) - max_show} more)")
        return "\n".join(lines)

    return run_clippy


def make_rust_tools(ctx: ToolContext) -> list:
    """Rust profile's tool bundle."""
    tools = list(make_generic_ast_tools(ctx))
    tools.append(make_run_clippy_tool(ctx))
    return tools
