"""Go profile — Tree-sitter AST + golangci-lint (100+ linters)."""

from ..base import ProfileBase, register


@register("go")
class GoProfile(ProfileBase):
    description = "Go (Tree-sitter AST + golangci-lint)"
    extensions = (".go",)
    languages = ("go",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Go.\n"
            "Common issue patterns to watch for:\n"
            "- SQL injection via fmt.Sprintf in db.Query (use parameterized: db.Query(?,?))\n"
            "- Command injection via exec.Command(\"sh\", \"-c\", userInput)\n"
            "- Path traversal: filepath.Join without filepath.Clean + boundary check\n"
            "- Race conditions: shared state without sync.Mutex / channels\n"
            "- nil pointer dereference: error returns silently ignored\n"
            "- defer in a loop accumulating until function returns (memory)\n"
            "- HTTP handlers without timeout configuration\n"
            "- crypto/rand vs math/rand: use the former for secrets\n"
            "- Missing context cancellation in long-running goroutines\n"
            "- Goroutine leaks: no exit path / no select on context.Done()\n"
            "- map iteration order is random — relying on it is a bug\n"
            "- Unchecked errors from io.Closer.Close, encoder.Encode, etc.\n"
            "\n"
            "Specialized tools available:\n"
            "- run_golangci_lint: bundles govet + staticcheck + gosec + errcheck + 100 more\n"
            "  (needs a go.mod somewhere in the path)\n"
            "- get_function_at / list_functions / list_classes / list_imports\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..go_runtime import make_go_tools_for_profile

        return make_go_tools_for_profile(ctx)
