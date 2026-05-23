"""Zig profile — LLM-only review."""

from ..base import ProfileBase, register


@register("zig")
class ZigProfile(ProfileBase):
    description = "Zig (LLM-only review)"
    extensions = (".zig",)
    languages = ("zig",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Zig.\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- Missing allocator passes — every dynamic allocation should accept an allocator\n"
            "- `defer` ordering: cleanup runs LIFO; `errdefer` only on error path\n"
            "- @ptrCast / @ptrFromInt without alignment audit\n"
            "- @intCast that can truncate without bounds check\n"
            "- Optional unwrap with .? — should the call site handle null instead?\n"
            "- Unsafe `@alignCast` / `@bitCast` between incompatible layouts\n"
            "- async/await used without an event loop installed\n"
            "- ArrayList.deinit not called → memory leak (Zig has no GC)\n"
            "- Test allocator not used in tests → leaks not caught\n"
            "- Build script (build.zig) hardcoded paths breaking cross-compilation\n"
            "- comptime values returning runtime data accidentally\n"
            "- Error-set unions over-broadened: anyerror hides specifics\n"
            "- Manual struct packing without @packed attribute\n"
            "- Standard library API churn: 0.11 → 0.12 → 0.13 frequent breaks\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
