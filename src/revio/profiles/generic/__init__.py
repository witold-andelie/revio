"""Generic profile — Tree-sitter AST tools for any supported language without
a dedicated Layer-2 static analyzer.

Activates for Java/Go/C/C++/Ruby/PHP/C#/Swift/Kotlin/Scala/Lua/SQL/Julia/Shell,
or any other future language whose grammar loads but doesn't have a profile
of its own.

Provides the same Layer 1 (AST) tools as the language-specific profiles, just
without language-specific Layer 2 (lint) coverage. The agent + LLM still
performs semantic review using its built-in language knowledge.
"""

from ..base import ProfileBase, register


@register("generic")
class GenericProfile(ProfileBase):
    description = "Tree-sitter AST tools for languages without a dedicated profile"
    # Intentionally empty — this profile activates by explicit selection
    # (--profile generic) or as the fallback in detect when no specific
    # profile matches. The actual handled extensions are dictated by
    # GenericTreeSitter.EXT_LANG (any grammar that loads).
    extensions = (
        ".java", ".kt", ".kts", ".scala", ".sc",
        ".go", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hxx",
        ".cs", ".rb", ".php", ".swift", ".lua", ".jl", ".sql",
        ".sh", ".bash", ".zsh",
    )
    languages = ("multi",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target: multi-language project (or a language without a dedicated profile).\n"
            "You have access to language-agnostic Tree-sitter tools that work for\n"
            "Python, Java, Go, Rust, C, C++, C#, Ruby, PHP, Lua, SQL, Julia, Scala,\n"
            "Kotlin, Swift, Shell, plus JS/TS — about 18 languages.\n"
            "\n"
            "Specialized tools available:\n"
            "- get_function_at / list_functions / list_classes / list_imports:\n"
            "  AST queries that work for any supported language\n"
            "- (No language-specific static linter — apply your domain knowledge\n"
            "   plus structural inspection via these AST tools.)\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile

        return make_generic_tools_for_profile(ctx)
