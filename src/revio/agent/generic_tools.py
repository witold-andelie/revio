"""Language-agnostic agent tools backed by GenericTreeSitter.

These work for ANY of the 18 supported languages (Python, Java, Rust, Go,
C, C++, C#, Ruby, PHP, Lua, SQL, Julia, Scala, Kotlin, Swift, Shell, plus JS/TS).

They're a less-deep counterpart to the JS-specific tools in js_tools.py:
- get_function_at(file, line)   — find the enclosing function
- list_functions(file)          — every function definition in the file
- list_classes(file)            — every class/struct/trait/interface
- list_imports(file)            — every import statement (raw text)

The JS-specific tools (call graph, symbol graph, dedup, oxlint) remain
exclusive to the JS profile.
"""

from __future__ import annotations

from langchain_core.tools import tool

from .tool_context import ToolContext


def make_get_function_at_generic_tool(ctx: ToolContext):
    @tool
    def get_function_at(relative_path: str, line: int) -> str:
        """Get the function/method/constructor enclosing a specific line.

        Works for any supported language (Python, Java, Rust, Go, C, C++, C#,
        Ruby, PHP, Lua, SQL, Julia, Scala, Kotlin, Swift, Shell, JS, TS).

        Use this instead of read_file when you want the EXACT function around
        a suspicious line — much more focused than reading the whole file.

        Args:
            relative_path: File path relative to repo root.
            line: 1-indexed line number.

        Returns:
            Function metadata + source body, or "(no function at that line)".
        """
        from ..layers.parser.treesitter_generic import shared as shared_ts

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        ts = shared_ts()
        if not ts.supports_path(full):
            return f"Error: language not supported for {relative_path} (no Tree-sitter grammar)."

        fn = ts.get_function_at(full, line)
        if fn is None:
            return f"(no function found at {relative_path}:{line})"

        cls = f"  enclosing class: {fn.enclosing_class}\n" if fn.enclosing_class else ""
        return (
            f"# Function at {relative_path}:{fn.line_start}-{fn.line_end}\n"
            f"  language: {ts.language_for_path(full)}\n"
            f"  kind: {fn.kind}\n"
            f"  name: {fn.name!r}\n"
            f"{cls}"
            f"  lines: {fn.line_count}\n"
            f"---\n{fn.body}"
        )

    return get_function_at


def make_list_functions_generic_tool(ctx: ToolContext):
    @tool
    def list_functions(relative_path: str) -> str:
        """List all functions / methods / constructors in a file.

        Cheaper than read_file when you only need an outline of what's where.

        Works for any supported language (18 grammars currently).

        Args:
            relative_path: File path relative to repo root.

        Returns:
            One line per function: `kind  name  Lstart-Lend  [in class]`
        """
        from ..layers.parser.treesitter_generic import shared as shared_ts

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        ts = shared_ts()
        if not ts.supports_path(full):
            return f"Error: language not supported for {relative_path}"

        functions = ts.list_functions(full)
        if not functions:
            return f"(no functions in {relative_path})"

        lines = [f"# Functions in {relative_path} ({len(functions)} total, lang={ts.language_for_path(full)})"]
        for fn in functions:
            cls = f" [in {fn.enclosing_class}]" if fn.enclosing_class else ""
            lines.append(
                f"  L{fn.line_start:4}-L{fn.line_end:<4}  {fn.kind:11}  {fn.name or '<anon>'}{cls}"
            )
        return "\n".join(lines)

    return list_functions


def make_list_classes_generic_tool(ctx: ToolContext):
    @tool
    def list_classes(relative_path: str) -> str:
        """List all classes/structs/traits/interfaces/etc. in a file.

        Args:
            relative_path: File path relative to repo root.

        Returns:
            One line per type: `kind  name  Lstart-Lend`
        """
        from ..layers.parser.treesitter_generic import shared as shared_ts

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        ts = shared_ts()
        if not ts.supports_path(full):
            return f"Error: language not supported for {relative_path}"

        classes = ts.list_classes(full)
        if not classes:
            return f"(no class-like definitions in {relative_path})"

        lines = [f"# Types in {relative_path} ({len(classes)} total)"]
        for c in classes:
            lines.append(f"  L{c.line_start:4}-L{c.line_end:<4}  {c.kind:11}  {c.name or '<anon>'}")
        return "\n".join(lines)

    return list_classes


def make_list_imports_generic_tool(ctx: ToolContext):
    @tool
    def list_imports(relative_path: str) -> str:
        """List all import / use / require statements in a file.

        Args:
            relative_path: File path relative to repo root.

        Returns:
            One line per import: `Lline: raw_statement_text`
        """
        from ..layers.parser.treesitter_generic import shared as shared_ts

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        ts = shared_ts()
        if not ts.supports_path(full):
            return f"Error: language not supported for {relative_path}"

        imports = ts.list_imports(full)
        if not imports:
            return f"(no imports in {relative_path})"

        lines = [f"# Imports in {relative_path} ({len(imports)} total)"]
        for imp in imports:
            lines.append(f"  L{imp.line:4}: {imp.text}")
        return "\n".join(lines)

    return list_imports


def make_generic_ast_tools(ctx: ToolContext) -> list:
    """All language-agnostic AST tools bound to a context."""
    return [
        make_get_function_at_generic_tool(ctx),
        make_list_functions_generic_tool(ctx),
        make_list_classes_generic_tool(ctx),
        make_list_imports_generic_tool(ctx),
    ]
