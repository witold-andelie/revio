"""Python profile — Tree-sitter AST + bandit (security linting)."""

from ..base import ProfileBase, register


@register("python")
class PythonProfile(ProfileBase):
    description = "Python (Tree-sitter AST + bandit security linter)"
    extensions = (".py", ".pyi")
    languages = ("python",)
    optional_dep_group = "python"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Python.\n"
            "Common issue patterns to watch for in this profile:\n"
            "- SQL injection via f-string / .format() in cursor.execute\n"
            "- Command injection via subprocess(shell=True)\n"
            "- Insecure deserialization (pickle.load, yaml.load without SafeLoader)\n"
            "- eval / exec on untrusted input\n"
            "- Weak crypto (hashlib.md5/sha1 for passwords, random for secrets)\n"
            "- Hardcoded secrets, API keys, database URLs with passwords\n"
            "- Path traversal via os.path.join with user input\n"
            "- Mutable default arguments\n"
            "- Bare except / except Exception silently swallowing errors\n"
            "- Missing context managers (open files, db connections)\n"
            "\n"
            "Specialized tools available in this profile:\n"
            "- run_bandit: security linting (use first to surface known issues)\n"
            "- get_function_at / list_functions / list_classes / list_imports:\n"
            "  language-agnostic AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..python_runtime import make_python_tools_for_profile

        return make_python_tools_for_profile(ctx)
