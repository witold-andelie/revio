"""SQL profile — Tree-sitter AST + sqlfluff."""

from ..base import ProfileBase, register


@register("sql")
class SqlProfile(ProfileBase):
    description = "SQL (Tree-sitter AST + sqlfluff, multi-dialect)"
    extensions = (".sql", ".ddl", ".dml")
    languages = ("sql",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: SQL (multi-dialect — postgres / mysql / snowflake / etc.).\n"
            "Common issue patterns to watch for:\n"
            "- Missing WHERE clauses on UPDATE / DELETE (production-killer)\n"
            "- Implicit cross joins (FROM a, b without ON)\n"
            "- SELECT * in production code (schema-drift risk)\n"
            "- NULL handling: NOT IN with NULL list, = NULL instead of IS NULL\n"
            "- Functions on indexed columns in WHERE (index bypass)\n"
            "- DDL without IF EXISTS / IF NOT EXISTS in migrations\n"
            "- Dialect mismatches (RETURNING is postgres-only, TOP is sqlserver-only)\n"
            "- Hardcoded values that should be parameters (SQL injection risk)\n"
            "\n"
            "Specialized tools available:\n"
            "- run_sqlfluff: SQL linter with dialect detection (call first)\n"
            "- get_function_at / list_functions: AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_sqlfluff_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_sqlfluff_tool(ctx)]
