"""PHP profile — Tree-sitter AST + phpstan."""

from ..base import ProfileBase, register


@register("php")
class PhpProfile(ProfileBase):
    description = "PHP (Tree-sitter AST + phpstan)"
    extensions = (".php", ".phtml")
    languages = ("php",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: PHP.\n"
            "Common issue patterns to watch for:\n"
            "- SQL injection via string concat in mysqli_query / PDO->query\n"
            "- XSS: unescaped echo of $_GET / $_POST / $_REQUEST\n"
            "- unserialize() on untrusted data (RCE)\n"
            "- eval() / create_function() — deprecated, prefer closures\n"
            "- File inclusion (include $_GET[...]) → RFI/LFI\n"
            "- Insecure password storage (md5/sha1 → use password_hash)\n"
            "- Loose comparison (== vs ===) on auth checks\n"
            "- Missing CSRF tokens in form handlers\n"
            "- Type juggling: '0e123' == '0e456' is true\n"
            "- Deprecated mysql_* extension usage\n"
            "\n"
            "Specialized tools available:\n"
            "- run_phpstan: deep static analysis (level 5 default; call first)\n"
            "- get_function_at / list_functions: AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_phpstan_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_phpstan_tool(ctx)]
