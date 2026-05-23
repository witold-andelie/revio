"""Rust profile — Tree-sitter AST + clippy (cargo linter)."""

from ..base import ProfileBase, register


@register("rust")
class RustProfile(ProfileBase):
    description = "Rust (Tree-sitter AST + cargo clippy)"
    extensions = (".rs",)
    languages = ("rust",)
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Rust.\n"
            "Common issue patterns to watch for in this profile:\n"
            "- `unsafe` blocks without a clear soundness argument\n"
            "- `unwrap()` / `expect()` in production code paths (panic risk)\n"
            "- `Rc<RefCell<>>` / shared mutable state — usually a sign of bad design\n"
            "- `.clone()` in hot loops (performance regression)\n"
            "- Manually implementing traits that #[derive] could provide\n"
            "- Lifetimes that could be simplified or elided\n"
            "- Integer arithmetic without overflow handling (i32::wrapping_add etc.)\n"
            "- Raw `*const T` / `*mut T` without null checks\n"
            "- `transmute` (almost always wrong)\n"
            "- `panic!`/`todo!`/`unimplemented!` left in production\n"
            "- `Result<_, ()>` (lossy error — use a real error type)\n"
            "\n"
            "Specialized tools available in this profile:\n"
            "- run_clippy: 600+ Rust lints (use first if Cargo.toml present)\n"
            "- get_function_at / list_functions / list_classes / list_imports:\n"
            "  language-agnostic AST queries via Tree-sitter\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..rust_runtime import make_rust_tools_for_profile

        return make_rust_tools_for_profile(ctx)
