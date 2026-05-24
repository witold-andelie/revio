"""Shell profile — Tree-sitter AST + shellcheck."""

from ..base import ProfileBase, register


@register("shell")
class ShellProfile(ProfileBase):
    description = "Shell scripts (Tree-sitter AST + shellcheck)"
    extensions = (".sh", ".bash", ".zsh")
    languages = ("shell", "bash")
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Shell (bash/sh/zsh).\n"
            "Common issue patterns to watch for:\n"
            "- Unquoted variables → word-splitting / glob expansion vulns\n"
            "- $? lost after pipe / subshell\n"
            "- 'set -e' interactions with command substitution\n"
            "- Race conditions on /tmp without mktemp\n"
            "- eval / exec on user-controlled strings\n"
            "- Missing 'set -u' / 'set -o pipefail'\n"
            "- exit-code masking with `command || true`\n"
            "\n"
            "Specialized tools available:\n"
            "- run_shellcheck: bash/sh/zsh static analysis (call first)\n"
            "- get_function_at / list_functions: AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_shellcheck_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_shellcheck_tool(ctx)]
