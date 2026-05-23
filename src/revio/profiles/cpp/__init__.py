"""C / C++ profile — Tree-sitter AST + cppcheck (static analyzer)."""

from ..base import ProfileBase, register


@register("cpp")
class CppProfile(ProfileBase):
    description = "C / C++ (Tree-sitter AST + cppcheck)"
    extensions = (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hxx")
    languages = ("c", "cpp")
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: C / C++.\n"
            "Common issue patterns to watch for (memory safety paramount):\n"
            "- Buffer overflows: strcpy, sprintf, gets, memcpy with wrong sizes\n"
            "- Use-after-free: free()'d pointer dereferenced later\n"
            "- Double-free: same pointer freed twice\n"
            "- Memory leaks: malloc without matching free, missing destructor in C++\n"
            "- Null pointer dereference: malloc return not checked\n"
            "- Uninitialized variable read: int x; ... = x;\n"
            "- Integer overflow: arithmetic on signed ints without bounds check\n"
            "- Format-string vulnerabilities: printf(user_str)\n"
            "- Off-by-one in array index / loop bound\n"
            "- Race conditions: shared state without std::mutex / atomic\n"
            "- C++: raw new/delete instead of unique_ptr/shared_ptr\n"
            "- C++: missing virtual destructor in polymorphic base class\n"
            "- C++: rule of three/five violated\n"
            "- C: scanf(\"%s\", buf) — no length limit, classic overflow\n"
            "\n"
            "Specialized tools available:\n"
            "- run_cppcheck: detects buffer / null / use-after-free / uninitvar / etc.\n"
            "  Output flags critical-severity CWEs (CWE-119, CWE-416, CWE-476).\n"
            "- get_function_at / list_functions / list_classes / list_imports\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..cpp_runtime import make_cpp_tools_for_profile

        return make_cpp_tools_for_profile(ctx)
