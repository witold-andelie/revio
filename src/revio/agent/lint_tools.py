"""Generic auto-emit tool factories for the 6 phase-2 static analyzers
(shellcheck / luacheck / sqlfluff / rubocop / phpstan / detekt).

All six share the same shape:
  - Locate runner on ctx (lazy property; None if binary missing)
  - Resolve relative_path under repo_root, refuse escape
  - Call runner.scan_to_findings()
  - Auto-emit into ctx.pending_findings so the agent doesn't need to re-call
    report_finding for each one
  - Return a human-readable summary string for the LLM
"""

from __future__ import annotations

from langchain_core.tools import tool

from .tool_context import ToolContext


def _make_lint_tool(
    ctx: ToolContext,
    tool_name: str,
    docstring: str,
    runner_attr: str,
    install_hint: str,
):
    """Factory that builds one auto-emit lint tool. Captured by closure.

    runner_attr: the ctx.<attr> property name returning the Runner or None
    """

    @tool(tool_name, description=docstring)
    def lint_tool(relative_path: str = ".") -> str:
        runner = getattr(ctx, runner_attr)
        if runner is None:
            return f"Error: {tool_name.replace('run_', '')} not installed. {install_hint}"

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
            return f"Error running {tool_name}: {e}"

        if not findings:
            return f"(no {tool_name.replace('run_', '')} issues in {relative_path})"

        ctx.pending_findings.extend(findings)
        max_show = 50
        lines = [
            f"{tool_name} findings in {relative_path} ({len(findings)} total — "
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

    return lint_tool


# --- Per-language factories --------------------------------------------------


def make_shellcheck_tool(ctx: ToolContext):
    return _make_lint_tool(
        ctx,
        tool_name="run_shellcheck",
        docstring=(
            "Run shellcheck on a Bash/sh/zsh file or directory. Surfaces "
            "quoting bugs, glob mistakes, subshell scope errors, $IFS pitfalls.\n\n"
            "Args:\n  relative_path: path under repo root. Default '.'\n\n"
            "Returns: one line per finding: file:line [severity] code message"
        ),
        runner_attr="shellcheck",
        install_hint="Install: brew install shellcheck / apt install shellcheck",
    )


def make_luacheck_tool(ctx: ToolContext):
    return _make_lint_tool(
        ctx,
        tool_name="run_luacheck",
        docstring=(
            "Run luacheck on a Lua file or directory. Surfaces unused "
            "variables, shadowing, global pollution, control-flow issues.\n\n"
            "Args:\n  relative_path: path under repo root. Default '.'\n\n"
            "Returns: one line per finding."
        ),
        runner_attr="luacheck",
        install_hint="Install: luarocks install luacheck",
    )


def make_sqlfluff_tool(ctx: ToolContext):
    return _make_lint_tool(
        ctx,
        tool_name="run_sqlfluff",
        docstring=(
            "Run sqlfluff on a SQL file or directory. Multi-dialect "
            "(postgres/mysql/snowflake/...) — set SQLFLUFF_DIALECT env. "
            "Catches parse errors, style violations, capitalisation.\n\n"
            "Args:\n  relative_path: path under repo root. Default '.'\n\n"
            "Returns: one line per finding."
        ),
        runner_attr="sqlfluff",
        install_hint="Install: pip install sqlfluff",
    )


def make_rubocop_tool(ctx: ToolContext):
    return _make_lint_tool(
        ctx,
        tool_name="run_rubocop",
        docstring=(
            "Run rubocop on a Ruby file or directory. Style + security + "
            "performance cops; auto-fix hints when available.\n\n"
            "Args:\n  relative_path: path under repo root. Default '.'\n\n"
            "Returns: one line per finding."
        ),
        runner_attr="rubocop",
        install_hint="Install: gem install rubocop",
    )


def make_phpstan_tool(ctx: ToolContext):
    return _make_lint_tool(
        ctx,
        tool_name="run_phpstan",
        docstring=(
            "Run phpstan on a PHP file or directory. Deep static analysis; "
            "catches type errors, dead code, deprecated API, security smells. "
            "Level defaults to 5; override via REVIO_PHPSTAN_LEVEL env.\n\n"
            "Args:\n  relative_path: path under repo root. Default '.'\n\n"
            "Returns: one line per finding."
        ),
        runner_attr="phpstan",
        install_hint="Install: composer global require phpstan/phpstan",
    )


def make_detekt_tool(ctx: ToolContext):
    return _make_lint_tool(
        ctx,
        tool_name="run_detekt",
        docstring=(
            "Run detekt on a Kotlin file or directory. Style + complexity + "
            "potential bugs + naming conventions. Requires a JDK.\n\n"
            "Args:\n  relative_path: path under repo root. Default '.'\n\n"
            "Returns: one line per finding."
        ),
        runner_attr="detekt",
        install_hint="Install: brew install detekt (macOS) or download detekt-cli",
    )
