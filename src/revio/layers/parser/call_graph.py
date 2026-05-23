"""JavaScript / TypeScript call graph.

For each parsed file, walk every function body and collect call-site information:
- `foo()`              → callee = "foo"
- `obj.foo()`          → callee = "foo" (member receiver tracked separately)
- `new Foo()`          → callee = "Foo" (recorded as constructor invocation)
- `await foo()`        → callee = "foo"

We then aggregate across files using the SymbolGraph to find:
- Which functions are called exactly once (single-call wrappers — dedup candidates)
- Which exported functions are never called from within the project (dead code)
- All call sites for a given symbol name

Limitations (M2 scope):
- Resolution is name-based, not type-based. `obj.foo()` matches any `foo` def.
- Aliased imports (`import { foo as bar }`) are tracked by the alias name.
- Computed property access (`obj["foo"]()`) is not resolved.
- Higher-order patterns (`fns[i]()`) are not resolved.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .symbol_graph import FileSymbols, SymbolGraph
from .treesitter_js import FunctionInfo, JSTreeSitter, _ParsedFile


logger = logging.getLogger(__name__)


# Node types that represent call sites
_CALL_NODE_TYPES = {"call_expression", "new_expression"}


# --- Models -------------------------------------------------------------------


@dataclass
class CallSite:
    """One call expression in the source."""

    file: Path                     # file containing the call
    line: int                      # 1-indexed
    callee_name: str               # the function being called (last identifier)
    callee_text: str               # full text: "foo" or "obj.foo" or "Foo.bar.baz"
    receiver: str | None           # "obj" for "obj.foo()"; None for "foo()"
    is_constructor: bool           # new X() invocation
    enclosing_function: str | None  # name of the function this call is inside


@dataclass
class FunctionStats:
    """Per-function aggregates for dedup/audit."""

    file: Path
    function: FunctionInfo
    call_sites: list[CallSite] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.call_sites)

    @property
    def fqn(self) -> str:
        """A semi-unique identifier for the function."""
        cls = f"{self.function.enclosing_class}." if self.function.enclosing_class else ""
        return f"{self.file.name}::{cls}{self.function.name or '<anon>'}@L{self.function.line_start}"


# --- CallGraph ----------------------------------------------------------------


class CallGraph:
    """Aggregate call-site index built on top of a SymbolGraph."""

    def __init__(self, symbol_graph: SymbolGraph):
        self.symbol_graph = symbol_graph
        self.parser = symbol_graph.parser

        # All call sites discovered, keyed by callee_name → list
        self.calls_by_name: dict[str, list[CallSite]] = defaultdict(list)
        # All call sites per file
        self.calls_by_file: dict[Path, list[CallSite]] = defaultdict(list)
        # Function index: (file, FunctionInfo) → stats
        self.function_stats: dict[tuple[Path, str], FunctionStats] = {}

    # ---- Building ----

    @classmethod
    def build(cls, symbol_graph: SymbolGraph) -> "CallGraph":
        graph = cls(symbol_graph)
        for fs in symbol_graph.files.values():
            graph._index_file(fs)
        graph._aggregate()
        return graph

    def _index_file(self, fs: FileSymbols) -> None:
        parsed = self.parser.parse_file(fs.path)
        if parsed is None:
            return

        # Build a map: line range → function name, so we can attribute call sites
        # to their enclosing function in source order.
        # Each function gets a stats entry.
        for fn in fs.functions:
            key = (fs.path, _fn_key(fn))
            self.function_stats[key] = FunctionStats(file=fs.path, function=fn)

        # Walk every call_expression / new_expression in the file
        for node in _iter(parsed.tree.root_node):
            if node.type not in _CALL_NODE_TYPES:
                continue
            site = self._make_call_site(node, fs, parsed)
            if site is None:
                continue
            self.calls_by_name[site.callee_name].append(site)
            self.calls_by_file[fs.path].append(site)

    def _make_call_site(self, node, fs: FileSymbols, parsed: _ParsedFile) -> CallSite | None:
        # Callee is the first child (the "function" being invoked)
        callee_node = None
        for child in node.children:
            if child.type in (
                "identifier", "member_expression", "subscript_expression",
                "parenthesized_expression", "this", "super",
            ):
                callee_node = child
                break
        if callee_node is None:
            return None

        callee_text = parsed.source[callee_node.start_byte:callee_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        # Final identifier in the chain
        name, receiver = _split_callee(callee_text)
        if not name:
            return None

        line = node.start_point[0] + 1
        enclosing = self._enclosing_function_name(fs, line)

        return CallSite(
            file=fs.path,
            line=line,
            callee_name=name,
            callee_text=callee_text,
            receiver=receiver,
            is_constructor=(node.type == "new_expression"),
            enclosing_function=enclosing,
        )

    @staticmethod
    def _enclosing_function_name(fs: FileSymbols, line: int) -> str | None:
        # Pick the innermost function whose range covers `line`
        best: FunctionInfo | None = None
        best_span = float("inf")
        for fn in fs.functions:
            if fn.line_start <= line <= fn.line_end:
                span = fn.line_end - fn.line_start
                if span < best_span:
                    best = fn
                    best_span = span
        return best.name if best else None

    def _aggregate(self) -> None:
        """Attach call sites to function stats by callee_name."""
        # Build (callee_name, file_set) → which functions could it refer to.
        # For each call site, find candidate function definitions:
        #   - same-file definition with matching name → highest confidence
        #   - imported symbol resolving to a file containing matching name
        #   - any export with that name (fallback, lower confidence)
        for name, sites in self.calls_by_name.items():
            for site in sites:
                # Find candidate definitions of `name`
                same_file = self._find_in_file(site.file, name)
                if same_file is not None:
                    key = (site.file, _fn_key(same_file))
                    if key in self.function_stats:
                        self.function_stats[key].call_sites.append(site)
                        continue

                # Try import resolution: what does `name` import from?
                # (For methods, we don't trace `obj.foo` — too ambiguous in M2.)
                if site.receiver is None:
                    resolved = self._resolve_via_imports(site.file, name)
                    if resolved is not None:
                        target_file, target_fn = resolved
                        key = (target_file, _fn_key(target_fn))
                        if key in self.function_stats:
                            self.function_stats[key].call_sites.append(site)
                            continue

                # Last-ditch: any export with that name (might be over-counting)
                defs = self.symbol_graph.get_definitions(name)
                if defs:
                    # Attach to the first matching FunctionInfo, if any
                    for target_file, _ex in defs:
                        target_fs = self.symbol_graph.get_file_symbols(target_file)
                        if target_fs is None:
                            continue
                        target_fn = next(
                            (fn for fn in target_fs.functions if fn.name == name), None
                        )
                        if target_fn is not None:
                            key = (target_file, _fn_key(target_fn))
                            if key in self.function_stats:
                                self.function_stats[key].call_sites.append(site)
                                break

    def _find_in_file(self, file: Path, name: str) -> FunctionInfo | None:
        fs = self.symbol_graph.get_file_symbols(file)
        if fs is None:
            return None
        for fn in fs.functions:
            if fn.name == name:
                return fn
        return None

    def _resolve_via_imports(self, file: Path, name: str) -> tuple[Path, FunctionInfo] | None:
        """If `name` is imported in `file`, find its definition."""
        fs = self.symbol_graph.get_file_symbols(file)
        if fs is None:
            return None

        for imp in fs.imports:
            # Direct named import: `import { name } from ...`
            if name in imp.imported_names or imp.default_name == name:
                # Resolve module
                for ri in self.symbol_graph.resolved_imports.get(file, []):
                    if ri.source == imp.source and ri.resolved is not None:
                        target_fs = self.symbol_graph.get_file_symbols(ri.resolved)
                        if target_fs is None:
                            continue
                        for fn in target_fs.functions:
                            if fn.name == name:
                                return ri.resolved, fn
        return None

    # ---- Query API ----

    def get_call_sites(self, name: str) -> list[CallSite]:
        """All call sites whose final identifier is `name`."""
        return list(self.calls_by_name.get(name, []))

    def get_function_stats(self, file: Path | str, function: FunctionInfo) -> FunctionStats | None:
        return self.function_stats.get((Path(file).resolve(), _fn_key(function)))

    def find_single_call_functions(self) -> list[FunctionStats]:
        """Functions called exactly once — dedup candidates (single-use wrappers)."""
        return [s for s in self.function_stats.values() if s.call_count == 1]

    def find_uncalled_functions(self, *, include_exported: bool = False) -> list[FunctionStats]:
        """Functions with zero in-project call sites.

        By default skips exported functions (they may be called by external code).
        Pass include_exported=True to flag them too.
        """
        out: list[FunctionStats] = []
        for stats in self.function_stats.values():
            if stats.call_count > 0:
                continue
            if not include_exported and stats.function.is_exported:
                continue
            # Skip constructors and anonymous functions (false positives)
            if stats.function.kind == "constructor":
                continue
            if stats.function.name is None:
                continue
            out.append(stats)
        return out

    def stats(self) -> dict[str, int]:
        return {
            "functions": len(self.function_stats),
            "call_sites": sum(len(v) for v in self.calls_by_name.values()),
            "uncalled_internal": len(self.find_uncalled_functions()),
            "single_call": len(self.find_single_call_functions()),
        }


# --- Helpers ------------------------------------------------------------------


def _iter(node):
    yield node
    for child in node.children:
        yield from _iter(child)


def _fn_key(fn: FunctionInfo) -> str:
    """Hashable identity for a function (within a file)."""
    return f"{fn.line_start}:{fn.line_end}:{fn.name or '<anon>'}:{fn.kind}"


def _split_callee(callee_text: str) -> tuple[str, str | None]:
    """Parse callee text into (final_name, receiver).

    Examples:
        "foo"            → ("foo", None)
        "obj.foo"        → ("foo", "obj")
        "a.b.c"          → ("c", "a.b")
        "(x => x)(1)"    → ("", None)   — IIFE not resolved
    """
    text = callee_text.strip()
    if "." not in text:
        # Plain identifier; reject anything with non-identifier chars
        if text.replace("_", "").isalnum() or text == "":
            return (text, None)
        return ("", None)

    parts = text.rsplit(".", 1)
    if len(parts) != 2:
        return ("", None)
    receiver, name = parts
    if not name.replace("_", "").isalnum():
        return ("", None)
    return (name, receiver)
