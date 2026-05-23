"""R profile — LLM-only review (no Tree-sitter grammar packaged on PyPI)."""

from ..base import ProfileBase, register


@register("r")
class RProfile(ProfileBase):
    description = "R (LLM-only review)"
    extensions = (".r", ".R", ".rmd", ".Rmd")
    languages = ("r",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: R.\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- eval(parse(text=x)) on user-controlled strings (code injection)\n"
            "- system / system2 / shell with user input (command injection)\n"
            "- source() of remote / untrusted files\n"
            "- SQL injection via paste/sprintf in DBI calls (use parameterized queries)\n"
            "- Implicit type coercion: 'a' < 1 silently succeeds (factor levels)\n"
            "- attach() polluting the global namespace\n"
            "- Mass-assignment via assign() / get() on dynamic names\n"
            "- Side effects from <<- (global assignment) in functions\n"
            "- options(stringsAsFactors=...) — historic gotcha, now default FALSE\n"
            "- NA propagation: sum(x) returns NA if any x is NA without na.rm=TRUE\n"
            "- Vectorization: explicit for-loops where apply / vapply / vectorized ops work\n"
            "- Random seed unset before reproducible analysis\n"
            "- library() calls inside functions (load-order dependence)\n"
            "- file.path / path.expand: no traversal guards on user paths\n"
            "- Stats: hypothesis testing without multiple-comparison correction\n"
            "- Plots saved without explicit dev.off() (file handle leak)\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
