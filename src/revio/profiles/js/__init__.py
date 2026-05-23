"""JavaScript / TypeScript profile — primary target of revio.

M2: full Layer 1 (Tree-sitter + symbol graph + call graph + function index)
and Layer 2 (oxlint subprocess) wired in via make_tools().
"""

from ..base import ProfileBase, register


@register("js")
class JSProfile(ProfileBase):
    description = "JavaScript / TypeScript / JSX / TSX (oxc + Tree-sitter)"
    extensions = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    languages = ("javascript", "typescript")
    optional_dep_group = "js"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: JavaScript / TypeScript.\n"
            "Common issue patterns to watch for in this profile:\n"
            "- Prototype pollution via Object.assign / spread on untrusted input\n"
            "- XSS via dangerouslySetInnerHTML, innerHTML, v-html, document.write\n"
            "- Unsafe template literal interpolation in SQL / shell / regex\n"
            "- Missing await on async functions (silent error swallow)\n"
            "- eval / Function constructor on untrusted input\n"
            "- Insecure JWT verification (algorithm: none, missing signature check)\n"
            "- Path traversal via path.join / fs without normalize\n"
            "- npm dependency confusion (package name typo-squatting)\n"
            "\n"
            "Specialized tools available in this profile:\n"
            "- run_oxlint: deterministic static analysis (use first to surface known issues)\n"
            "- get_function_at: pinpoint enclosing function for a specific line\n"
            "- list_functions / get_imports: file structure without full read\n"
            "- get_call_sites: trace who calls a given function (use for taint tracing)\n"
            "- find_similar_functions / find_duplicate_groups: dedup mode entry points\n"
            "- find_uncalled_functions: dead-code candidates\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        # Lazy import to avoid forcing tree-sitter deps when profile not active
        from ..js_runtime import make_js_tools_for_profile

        return make_js_tools_for_profile(ctx)
