"""Ruby profile — Tree-sitter AST + rubocop."""

from ..base import ProfileBase, register


@register("ruby")
class RubyProfile(ProfileBase):
    description = "Ruby (Tree-sitter AST + rubocop)"
    extensions = (".rb", ".rake", ".gemspec")
    languages = ("ruby",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Ruby.\n"
            "Common issue patterns to watch for:\n"
            "- String SQL building in ActiveRecord (use parameterized queries)\n"
            "- Mass-assignment without strong_params (Rails-specific)\n"
            "- send() / public_send() on user input (arbitrary method dispatch)\n"
            "- eval / instance_eval / class_eval on untrusted strings\n"
            "- YAML.load on untrusted input (use safe_load)\n"
            "- Marshal.load on untrusted input\n"
            "- Use of Object#method_missing without respond_to_missing?\n"
            "- Boolean nil pitfalls — nil is falsy but so is false\n"
            "- Mutation of frozen-by-convention constants\n"
            "\n"
            "Specialized tools available:\n"
            "- run_rubocop: Ruby linter (call first — covers style/security/perf cops)\n"
            "- get_function_at / list_functions: AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_rubocop_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_rubocop_tool(ctx)]
