"""Lua profile — Tree-sitter AST + luacheck."""

from ..base import ProfileBase, register


@register("lua")
class LuaProfile(ProfileBase):
    description = "Lua (Tree-sitter AST + luacheck)"
    extensions = (".lua",)
    languages = ("lua",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Lua.\n"
            "Common issue patterns to watch for:\n"
            "- Implicit globals from missing 'local' keyword\n"
            "- Shadowing of upvalues in nested closures\n"
            "- Misuse of pairs() vs ipairs() on sparse arrays\n"
            "- Forgotten 'return' in coroutine bodies\n"
            "- 1-based indexing pitfalls when interfacing with C arrays\n"
            "- pcall/xpcall not used around risky operations\n"
            "- Metamethod misuse (__index recursion, __newindex shadowing)\n"
            "\n"
            "Specialized tools available:\n"
            "- run_luacheck: Lua linter (call first)\n"
            "- get_function_at / list_functions: AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_luacheck_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_luacheck_tool(ctx)]
