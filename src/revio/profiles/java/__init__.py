"""Java profile — Tree-sitter AST + SpotBugs (bug + security analyzer)."""

from ..base import ProfileBase, register


@register("java")
class JavaProfile(ProfileBase):
    description = "Java (Tree-sitter AST + SpotBugs)"
    extensions = (".java",)
    languages = ("java",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Java.\n"
            "Common issue patterns to watch for:\n"
            "- SQL injection via Statement concatenation / String.format (use PreparedStatement)\n"
            "- XXE in DocumentBuilderFactory / SAXParserFactory (disable external entities)\n"
            "- Insecure deserialization (ObjectInputStream on untrusted data)\n"
            "- Hardcoded passwords / API keys in source\n"
            "- Path traversal via File constructor with user input\n"
            "- Weak crypto (MD5/SHA1 for passwords, ECB mode, DES)\n"
            "- NullPointerException risk: no @Nullable / Optional + dereferences\n"
            "- Resource leaks: missing try-with-resources for Closeable\n"
            "- == used for String comparison instead of .equals()\n"
            "- Mutable static fields (thread-safety + state leakage)\n"
            "- Spring: missing CSRF protection, exposed actuators, weak session config\n"
            "\n"
            "Specialized tools available:\n"
            "- run_spotbugs: bug + security analysis (NEEDS compiled .class files,\n"
            "  run `mvn compile` or `javac` first if you only have .java sources)\n"
            "- get_function_at / list_functions / list_classes / list_imports\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..java_runtime import make_java_tools_for_profile

        return make_java_tools_for_profile(ctx)
