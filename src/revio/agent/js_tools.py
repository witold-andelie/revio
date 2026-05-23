"""JS profile-specific agent tools (Layer 1 + Layer 2 backed).

Available to the agent when the active profile is `js`. Each tool returns
human-readable text (not structured data) because that's what flows back
into the LLM's message history.
"""

from __future__ import annotations

from langchain_core.tools import tool

from .tool_context import ToolContext


# --- Layer 2 — oxlint ---------------------------------------------------------


def make_run_oxlint_tool(ctx: ToolContext):
    """Return the run_oxlint tool bound to a context."""

    @tool
    def run_oxlint(relative_path: str = ".") -> str:
        """Run oxlint (Rust-based JS/TS static analyzer) on a file or directory.

        Use this to surface deterministic issues — unused imports, eval usage,
        duplicate keys, unreachable code, etc. — without spending LLM cycles
        on them.

        Args:
            relative_path: File or directory under the repo root. Default "." for the whole repo.

        Returns:
            One line per finding: `file:line  [severity]  rule_id  message`
            or "(no issues found)".
        """
        runner = ctx.oxlint
        if runner is None:
            return "Error: oxlint not installed. Install with `npm install -g oxlint`."

        target = (ctx.repo_root / relative_path).resolve()
        try:
            target.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        if not target.exists():
            return f"Error: not found: {relative_path}"

        try:
            findings = runner.lint_to_findings(target, repo_root=ctx.repo_root)
        except Exception as e:
            return f"Error running oxlint: {e}"

        if not findings:
            return f"(no oxlint issues in {relative_path})"

        # Auto-emit: push to ctx so react_node merges into state. The LLM
        # doesn't need to re-record these via report_finding.
        ctx.pending_findings.extend(findings)

        # Cap output to keep prompt budget reasonable
        max_show = 50
        lines = [
            f"oxlint findings in {relative_path} ({len(findings)} total — "
            f"auto-recorded, no need to call report_finding for these):",
        ]
        for f in findings[:max_show]:
            rule = f.evidence[0].source if f.evidence else "?"
            lines.append(
                f"  {f.file_path}:{f.line_start}  [{f.severity.value:8}]  "
                f"{rule}  {f.title[:80]}"
            )
        if len(findings) > max_show:
            lines.append(f"  ... ({len(findings) - max_show} more)")
        return "\n".join(lines)

    return run_oxlint


# --- Layer 1 — AST queries ----------------------------------------------------


def make_get_function_at_tool(ctx: ToolContext):
    @tool
    def get_function_at(relative_path: str, line: int) -> str:
        """Get the function containing a specific line (with full body and metadata).

        Use this instead of read_file when you want the EXACT function around
        a suspicious line — much more focused than reading the whole file.

        Args:
            relative_path: File path relative to repo root.
            line: 1-indexed line number.

        Returns:
            Function info + source body, or "(no function at that line)".
        """
        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        fn = ctx.symbol_graph.parser.get_function_at(full, line)
        if fn is None:
            return f"(no function found at {relative_path}:{line})"

        cls = f"  enclosing class: {fn.enclosing_class}\n" if fn.enclosing_class else ""
        params = ", ".join(fn.parameter_names) if fn.parameter_names else "(none)"
        return (
            f"# Function at {relative_path}:{fn.line_start}-{fn.line_end}\n"
            f"  kind: {fn.kind}\n"
            f"  name: {fn.name!r}\n"
            f"{cls}"
            f"  async: {fn.is_async}\n"
            f"  exported: {fn.is_exported}{' (default)' if fn.is_default_export else ''}\n"
            f"  parameters: {params}\n"
            f"  lines: {fn.line_count}\n"
            f"---\n{fn.body}"
        )

    return get_function_at


def make_list_functions_tool(ctx: ToolContext):
    @tool
    def list_functions(relative_path: str) -> str:
        """List all functions / methods / arrow functions in a file with their line ranges.

        Cheaper than read_file when you only need an outline of what's where.

        Args:
            relative_path: File path relative to repo root.

        Returns:
            One line per function: `kind  name  Lstart-Lend  [class]  [async] [exported]`
        """
        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        functions = ctx.symbol_graph.parser.list_functions(full)
        if not functions:
            return f"(no functions in {relative_path})"

        lines = [f"# Functions in {relative_path} ({len(functions)} total)"]
        for fn in functions:
            cls = f" [in class {fn.enclosing_class}]" if fn.enclosing_class else ""
            flags = []
            if fn.is_async:
                flags.append("async")
            if fn.is_exported:
                flags.append("exported")
            if fn.is_default_export:
                flags.append("default")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"  L{fn.line_start:4}-L{fn.line_end:4}  {fn.kind:11}  {fn.name or '<anon>'}{cls}{flag_str}"
            )
        return "\n".join(lines)

    return list_functions


def make_get_imports_tool(ctx: ToolContext):
    @tool
    def get_imports(relative_path: str) -> str:
        """List all import statements in a JS/TS file.

        Useful to understand a file's dependencies before deeper analysis.

        Args:
            relative_path: File path relative to repo root.

        Returns:
            One line per import: `line  source  [names...]`
        """
        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        imports = ctx.symbol_graph.parser.list_imports(full)
        if not imports:
            return f"(no imports in {relative_path})"

        lines = [f"# Imports in {relative_path} ({len(imports)} total)"]
        for imp in imports:
            parts: list[str] = []
            if imp.default_name:
                parts.append(f"default={imp.default_name}")
            if imp.namespace_name:
                parts.append(f"namespace=* as {imp.namespace_name}")
            if imp.imported_names:
                parts.append(f"names={{{', '.join(imp.imported_names)}}}")
            if imp.is_side_effect:
                parts.append("side-effect")
            detail = "  ".join(parts) if parts else ""
            lines.append(f"  L{imp.line:4}  {imp.source!r:30}  {detail}")
        return "\n".join(lines)

    return get_imports


# --- Cross-file: call graph ---------------------------------------------------


def make_get_call_sites_tool(ctx: ToolContext):
    @tool
    def get_call_sites(symbol_name: str, max_results: int = 30) -> str:
        """Find every call site of a function/method by name across the project.

        Use this to:
        - Trace how user input flows from handler → DB (in audit mode)
        - Identify single-call wrappers for dedup mode
        - Estimate blast radius of changing a function

        Note: name-only matching. `obj.foo()` matches any `foo` definition.

        Args:
            symbol_name: The function name to search for.
            max_results: Truncate output if too many call sites.

        Returns:
            One line per call site: `file:line  in <enclosing_fn>  callee_text`
        """
        sites = ctx.call_graph.get_call_sites(symbol_name)
        if not sites:
            return f"(no call sites found for '{symbol_name}')"

        lines = [f"# Call sites of '{symbol_name}' ({len(sites)} total)"]
        for s in sites[:max_results]:
            try:
                rel = str(s.file.relative_to(ctx.repo_root))
            except ValueError:
                rel = str(s.file)
            in_fn = f"in {s.enclosing_function}" if s.enclosing_function else "(module-level)"
            new_kw = "new " if s.is_constructor else ""
            lines.append(f"  {rel}:{s.line}  {in_fn}  →  {new_kw}{s.callee_text}")
        if len(sites) > max_results:
            lines.append(f"  ... ({len(sites) - max_results} more)")
        return "\n".join(lines)

    return get_call_sites


# --- Dedup mode tools ---------------------------------------------------------


def make_find_similar_functions_tool(ctx: ToolContext):
    @tool
    def find_similar_functions(
        relative_path: str,
        line: int,
        threshold: float = 0.85,
    ) -> str:
        """Find functions structurally similar to the one at the given location.

        Used in dedup mode to identify AI-generated duplicates. The result
        gives the LLM candidate pairs to semantically verify with read_file
        and reasoning.

        Args:
            relative_path: File path of the target function (relative to repo root).
            line: Any line inside the target function.
            threshold: Jaccard similarity 0-1 (0.85 default; 1.0 = exact structural match).

        Returns:
            Ranked list of similar functions.
        """
        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."

        target = ctx.function_index.get_by_location(full, line)
        if target is None:
            return f"(no indexed function at {relative_path}:{line} — may be too short)"

        # Exact structural duplicates first
        same_hash = [
            fp for fp in ctx.function_index.fingerprints
            if fp.structural_hash == target.structural_hash and fp is not target
        ]
        # Near-duplicates by Jaccard
        near = ctx.function_index.find_near_duplicates(target, threshold=threshold)

        if not same_hash and not near:
            return f"(no functions similar to {target.fqn})"

        lines = [f"# Functions similar to {target.fqn} ({target.token_count} tokens)"]
        if same_hash:
            lines.append(f"  -- structural duplicates ({len(same_hash)}) --")
            for fp in same_hash:
                lines.append(f"    {fp.fqn}  (identical normalized body)")
        if near:
            lines.append(f"  -- near-duplicates (Jaccard ≥ {threshold:.2f}) --")
            for fp, sim in near:
                lines.append(f"    sim={sim:.2f}  {fp.fqn}")
        return "\n".join(lines)

    return find_similar_functions


def make_find_duplicate_groups_tool(ctx: ToolContext):
    @tool
    def find_duplicate_groups(min_group_size: int = 2) -> str:
        """Repository-wide scan for groups of structurally identical functions.

        Output is the entry point for dedup mode: gives the agent every
        candidate group at once. The agent then picks the most promising
        groups, reads each member's source, and judges semantic equivalence.

        Args:
            min_group_size: Only show groups with at least this many members (default 2).

        Returns:
            One section per duplicate group with FQNs of every member.
        """
        groups = ctx.function_index.find_duplicate_groups(min_size=min_group_size)
        if not groups:
            return "(no duplicate function groups detected)"

        lines = [
            f"# Duplicate function groups ({len(groups)} total)",
            f"# Index stats: {ctx.function_index.stats()}",
        ]
        for i, g in enumerate(groups, 1):
            lines.append(f"\nGroup {i}: {g.count} members, hash={g.structural_hash[:12]}…")
            for m in g.members:
                lines.append(f"  · {m.fqn}  ({m.token_count} tokens)")
        return "\n".join(lines)

    return find_duplicate_groups


def make_find_uncalled_functions_tool(ctx: ToolContext):
    @tool
    def find_uncalled_functions(include_exported: bool = False) -> str:
        """List functions never called from within the project (dead code candidates).

        Args:
            include_exported: Also include exported functions (they may be called externally).

        Returns:
            FQN + location for each uncalled function.
        """
        uncalled = ctx.call_graph.find_uncalled_functions(include_exported=include_exported)
        if not uncalled:
            return "(no uncalled functions detected)"

        lines = [f"# Uncalled functions ({len(uncalled)} total)"]
        for stats in uncalled:
            try:
                rel = str(stats.file.relative_to(ctx.repo_root))
            except ValueError:
                rel = str(stats.file)
            fn = stats.function
            lines.append(
                f"  {rel}:{fn.line_start}-{fn.line_end}  {fn.kind} {fn.name}  "
                f"({fn.line_count} lines, exported={fn.is_exported})"
            )
        return "\n".join(lines)

    return find_uncalled_functions


# --- Bundle -------------------------------------------------------------------


def make_js_tools(ctx: ToolContext) -> list:
    """All JS profile tools bound to the given context."""
    return [
        make_run_oxlint_tool(ctx),
        make_get_function_at_tool(ctx),
        make_list_functions_tool(ctx),
        make_get_imports_tool(ctx),
        make_get_call_sites_tool(ctx),
        make_find_similar_functions_tool(ctx),
        make_find_duplicate_groups_tool(ctx),
        make_find_uncalled_functions_tool(ctx),
    ]
