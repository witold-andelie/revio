"""Kotlin profile — Tree-sitter AST + detekt."""

from ..base import ProfileBase, register


@register("kotlin")
class KotlinProfile(ProfileBase):
    description = "Kotlin (Tree-sitter AST + detekt; requires JDK)"
    extensions = (".kt", ".kts")
    languages = ("kotlin",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Kotlin.\n"
            "Common issue patterns to watch for:\n"
            "- !! operator usage where ?. or ?: would be safer\n"
            "- Nullable type mishandling (platform types from Java interop)\n"
            "- `lateinit` reads before initialization (UninitializedPropertyAccessException)\n"
            "- Coroutine scope leaks (GlobalScope.launch in production)\n"
            "- Blocking IO inside suspend functions without withContext(Dispatchers.IO)\n"
            "- Data class containing mutable fields (defeats equals/hashCode)\n"
            "- `object` singletons holding Android Context references (leaks)\n"
            "- `when` expressions without else on non-sealed types\n"
            "- Companion object misused for static-like access patterns\n"
            "- Suspending functions blocking the main dispatcher\n"
            "\n"
            "Specialized tools available:\n"
            "- run_detekt: Kotlin linter (call first; requires JDK installed)\n"
            "- get_function_at / list_functions: AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_detekt_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_detekt_tool(ctx)]
