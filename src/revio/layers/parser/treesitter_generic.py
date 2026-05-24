"""Language-agnostic AST extraction via Tree-sitter.

Ported from v1's ast_extractor.py and adapted for revio. The original v1
class is essentially language-table-driven — same algorithm, different node
type names per language. We keep that structure.

What this provides for ANY of the 18 supported languages:
- get_function_at(file, line)  — find enclosing function/method
- list_functions(file)         — every function definition with name + range
- list_classes(file)           — every class/struct/trait/etc.
- list_imports(file)           — every import statement (raw text)
- get_enclosing_class(file, line) — class containing the line
- get_node_text(node)          — source text for any node

JS-specific deep features (cross-file imports/exports, call graphs, dedup
fingerprints) remain in treesitter_js.py and the symbol_graph / call_graph /
function_index modules — those need language-specific knowledge that doesn't
generalize cheaply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel

from .language_support import shared as shared_languages


logger = logging.getLogger(__name__)


# --- Per-language node type tables (ported from v1's ASTExtractor) -----------
# These map our language tag → list of Tree-sitter node types that represent
# "function definition", "class definition", and "import statement" in that
# language's grammar.

_FUNCTION_TYPES: dict[str, list[str]] = {
    "python":       ["function_definition"],
    "javascript":   ["function_declaration", "method_definition", "arrow_function",
                     "generator_function_declaration", "function_expression"],
    "typescript":   ["function_declaration", "method_definition", "arrow_function",
                     "generator_function_declaration", "function_expression",
                     "function_signature"],
    "tsx":          ["function_declaration", "method_definition", "arrow_function",
                     "function_signature"],
    "c_sharp":      ["method_declaration", "constructor_declaration",
                     "local_function_statement"],
    "java":         ["method_declaration", "constructor_declaration"],
    "go":           ["function_declaration", "method_declaration"],
    "rust":         ["function_item"],
    "c":            ["function_definition"],
    "cpp":          ["function_definition"],
    "php":          ["function_definition", "method_declaration"],
    "ruby":         ["method", "singleton_method"],
    "swift":        ["function_declaration"],
    "kotlin":       ["function_declaration"],
    "scala":        ["function_definition", "val_definition"],
    "lua":          ["function_declaration", "function_definition"],
    "sql":          ["create_function_statement"],
    "julia":        ["function_definition", "short_function_definition"],
    "shell":        ["function_definition"],
    # Future (no PyPI grammar yet):
    "matlab":       ["function_definition"],
    "solidity":     ["function_definition", "modifier_definition"],
    "verilog":      ["function_declaration", "task_declaration"],
    "zig":          ["function_declaration"],
    "objective_c":  ["function_definition", "method_definition"],
}


_CLASS_TYPES: dict[str, list[str]] = {
    "python":       ["class_definition"],
    "javascript":   ["class_declaration"],
    "typescript":   ["class_declaration", "interface_declaration"],
    "tsx":          ["class_declaration", "interface_declaration"],
    "c_sharp":      ["class_declaration", "struct_declaration",
                     "interface_declaration", "record_declaration"],
    "java":         ["class_declaration", "interface_declaration",
                     "enum_declaration"],
    "go":           ["type_declaration"],
    "rust":         ["impl_item", "struct_item", "trait_item", "enum_item"],
    "c":            ["struct_specifier"],
    "cpp":          ["class_specifier", "struct_specifier"],
    "php":          ["class_declaration", "interface_declaration",
                     "trait_declaration"],
    "ruby":         ["class", "module"],
    "swift":        ["class_declaration", "struct_declaration",
                     "protocol_declaration"],
    "kotlin":       ["class_declaration", "object_declaration"],
    "scala":        ["class_definition", "object_definition",
                     "trait_definition"],
    "lua":          [],
    "sql":          [],
    "julia":        ["abstract_definition", "struct_definition"],
    "shell":        [],
    "matlab":       [],
    "solidity":     ["contract_declaration", "interface_declaration",
                     "library_declaration"],
    "verilog":      ["module_declaration", "class_declaration",
                     "interface_declaration", "package_declaration"],
    "zig":          [],
    "objective_c":  ["class_interface", "class_implementation"],
}


_IMPORT_TYPES: dict[str, list[str]] = {
    "python":       ["import_statement", "import_from_statement"],
    "javascript":   ["import_statement"],
    "typescript":   ["import_statement"],
    "tsx":          ["import_statement"],
    "c_sharp":      ["using_directive"],
    "java":         ["import_declaration"],
    "go":           ["import_declaration"],
    "rust":         ["use_declaration"],
    "c":            ["preproc_include"],
    "cpp":          ["preproc_include"],
    "php":          ["namespace_use_declaration"],
    "ruby":         [],  # require/require_relative are method calls, not statements
    "swift":        ["import_declaration"],
    "kotlin":       ["import_header"],
    "scala":        ["import_declaration"],
    "lua":          [],
    "sql":          [],
    "julia":        ["import_statement"],
    "shell":        [],
    "matlab":       [],
    "solidity":     ["import_directive"],
    "verilog":      [],
    "zig":          [],
    "objective_c":  ["preproc_import", "import"],
}


# Identifier-like node types used when extracting a function/class name.
_NAME_NODE_TYPES = {"name", "identifier", "type_identifier",
                    "simple_identifier", "property_identifier"}


# --- Models ------------------------------------------------------------------


class FunctionDef(BaseModel):
    """A function/method/constructor definition in any language."""
    name: str | None = None
    kind: str = "function"           # function | method | constructor | other
    line_start: int                   # 1-indexed
    line_end: int
    enclosing_class: str | None = None
    body: str = ""                    # full source text

    @property
    def line_count(self) -> int:
        return self.line_end - self.line_start + 1


class ClassDef(BaseModel):
    """A class/struct/trait/interface definition."""
    name: str | None = None
    kind: str = "class"               # class | struct | trait | interface | ...
    line_start: int
    line_end: int
    body: str = ""


class ImportStmt(BaseModel):
    """A raw import statement (text only — semantics are language-specific)."""
    line: int
    text: str


# --- Per-file parsed result (cached) -----------------------------------------


@dataclass
class _ParsedFile:
    tree: object              # tree_sitter.Tree
    source: bytes
    language: str
    mtime: float


# --- GenericTreeSitter -------------------------------------------------------


# Extension → internal language tag.
# This is the canonical table for routing files to the right Tree-sitter
# grammar. Update here when adding a new language.
EXT_LANG: dict[str, str] = {
    # JS family
    ".js":      "javascript",
    ".jsx":     "javascript",
    ".mjs":     "javascript",
    ".cjs":     "javascript",
    ".ts":      "typescript",
    ".tsx":     "tsx",
    # Python
    ".py":      "python",
    ".pyi":     "python",
    # JVM family
    ".java":    "java",
    ".kt":      "kotlin",
    ".kts":     "kotlin",
    ".scala":   "scala",
    ".sc":      "scala",
    # Systems languages
    ".go":      "go",
    ".rs":      "rust",
    ".c":       "c",
    ".h":       "c",
    ".cpp":     "cpp",
    ".cc":      "cpp",
    ".cxx":     "cpp",
    ".hpp":     "cpp",
    ".hxx":     "cpp",
    ".cs":      "c_sharp",
    # Dynamic
    ".rb":      "ruby",
    ".php":     "php",
    ".swift":   "swift",
    ".lua":     "lua",
    ".jl":      "julia",
    # SQL / Shell
    ".sql":     "sql",
    ".sh":      "shell",
    ".bash":    "shell",
    ".zsh":     "shell",
    # Hardware description (Verilog / SystemVerilog)
    ".v":       "verilog",
    ".vh":      "verilog",
    ".sv":      "verilog",
    ".svh":     "verilog",
}


class GenericTreeSitter:
    """Tree-sitter wrapper that works for any of the supported languages."""

    def __init__(self, cache_size: int = 128):
        self.cache_size = cache_size
        self._cache: dict[Path, _ParsedFile] = {}

    # ---- Detection ----

    @staticmethod
    def language_for_path(path: Path | str) -> str | None:
        path = Path(path)
        return EXT_LANG.get(path.suffix.lower())

    # ---- Parsing (cached by mtime) ----

    def parse_file(self, path: Path | str) -> _ParsedFile | None:
        """Parse a file with the right grammar. None if unsupported."""
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            return None

        language = self.language_for_path(path)
        if language is None:
            return None

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        cached = self._cache.get(path)
        if cached and cached.mtime == mtime and cached.language == language:
            return cached

        parser = shared_languages().get_parser(language)
        if parser is None:
            return None

        try:
            source = path.read_bytes()
        except OSError:
            return None

        tree = parser.parse(source)
        parsed = _ParsedFile(tree=tree, source=source, language=language, mtime=mtime)
        self._evict_if_needed()
        self._cache[path] = parsed
        return parsed

    def supports_path(self, path: Path | str) -> bool:
        lang = self.language_for_path(path)
        if lang is None:
            return False
        return shared_languages().is_supported(lang)

    # ---- Function queries ----

    def list_functions(self, path: Path | str) -> list[FunctionDef]:
        parsed = self.parse_file(path)
        if parsed is None:
            return []

        types = set(_FUNCTION_TYPES.get(parsed.language, []))
        if not types:
            return []

        results: list[FunctionDef] = []
        for node, enclosing_class in self._walk_with_class(parsed.tree.root_node, parsed.language):
            if node.type not in types:
                continue
            results.append(self._function_def(node, parsed, enclosing_class))
        return results

    def get_function_at(self, path: Path | str, line: int) -> FunctionDef | None:
        """Innermost function/method whose range encloses 1-indexed `line`."""
        parsed = self.parse_file(path)
        if parsed is None:
            return None

        types = set(_FUNCTION_TYPES.get(parsed.language, []))
        if not types:
            return None

        target = line - 1  # tree-sitter rows are 0-indexed
        best: tuple[object, str | None] | None = None
        for node, enclosing in self._walk_with_class(parsed.tree.root_node, parsed.language):
            if node.type not in types:
                continue
            if node.start_point[0] <= target <= node.end_point[0]:
                # Smaller spans (more inner) win
                if best is None or self._span(node) < self._span(best[0]):
                    best = (node, enclosing)
        if best is None:
            return None
        return self._function_def(best[0], parsed, best[1])

    # ---- Class queries ----

    def list_classes(self, path: Path | str) -> list[ClassDef]:
        parsed = self.parse_file(path)
        if parsed is None:
            return []
        types = set(_CLASS_TYPES.get(parsed.language, []))
        if not types:
            return []
        # Rust impl blocks provide enclosing-class context for methods but
        # aren't user-visible class definitions on their own. Exclude them
        # from the listing.
        skip_in_listing = {"impl_item"}
        results: list[ClassDef] = []
        for node in self._iter(parsed.tree.root_node):
            if node.type not in types:
                continue
            if node.type in skip_in_listing:
                continue
            results.append(self._class_def(node, parsed))
        return results

    def get_enclosing_class(self, path: Path | str, line: int) -> ClassDef | None:
        parsed = self.parse_file(path)
        if parsed is None:
            return None
        types = set(_CLASS_TYPES.get(parsed.language, []))
        if not types:
            return None
        target = line - 1
        best: object | None = None
        for node in self._iter(parsed.tree.root_node):
            if node.type not in types:
                continue
            if node.start_point[0] <= target <= node.end_point[0]:
                if best is None or self._span(node) < self._span(best):
                    best = node
        return self._class_def(best, parsed) if best else None

    # ---- Import queries ----

    def list_imports(self, path: Path | str) -> list[ImportStmt]:
        parsed = self.parse_file(path)
        if parsed is None:
            return []
        types = set(_IMPORT_TYPES.get(parsed.language, []))
        if not types:
            return []
        results: list[ImportStmt] = []
        for node in self._iter(parsed.tree.root_node):
            if node.type not in types:
                continue
            text = parsed.source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            results.append(ImportStmt(line=node.start_point[0] + 1, text=text.strip()))
        return results

    # ---- Internal helpers ----

    def _iter(self, node) -> Iterator:
        yield node
        for child in node.children:
            yield from self._iter(child)

    def _walk_with_class(self, root, language: str) -> Iterator:
        """Pre-order traversal yielding (node, enclosing_class_name_or_None)."""
        class_types = set(_CLASS_TYPES.get(language, []))
        stack: list[tuple[object, str | None]] = [(root, None)]
        while stack:
            node, enclosing = stack.pop()
            yield node, enclosing
            if node.type in class_types:
                klass_name = self._extract_name(node)
                for c in reversed(node.children):
                    stack.append((c, klass_name))
            else:
                for c in reversed(node.children):
                    stack.append((c, enclosing))

    def _function_def(self, node, parsed: _ParsedFile, enclosing_class: str | None) -> FunctionDef:
        name = self._extract_name(node)
        kind = "function"
        if node.type in ("method_declaration", "method_definition", "method", "singleton_method"):
            kind = "method"
        elif node.type in ("constructor_declaration",):
            kind = "constructor"
        elif node.type == "arrow_function":
            kind = "arrow"
        body = parsed.source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        return FunctionDef(
            name=name,
            kind=kind,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            enclosing_class=enclosing_class,
            body=body,
        )

    def _class_def(self, node, parsed: _ParsedFile) -> ClassDef:
        name = self._extract_name(node)
        # Kind heuristics by node type
        kind = "class"
        nt = node.type
        if "struct" in nt:
            kind = "struct"
        elif "trait" in nt:
            kind = "trait"
        elif "interface" in nt:
            kind = "interface"
        elif "enum" in nt:
            kind = "enum"
        elif "object" in nt:
            kind = "object"
        elif "module" in nt:
            kind = "module"
        elif nt == "record_declaration":
            kind = "record"
        body = parsed.source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        return ClassDef(
            name=name,
            kind=kind,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            body=body,
        )

    def _extract_name(self, node) -> str | None:
        """Try every common pattern to extract a function/class name.

        We try tree-sitter's `child_by_field_name("name")` first — most modern
        grammars (Java, Go, Rust, C#, etc.) expose the name as a labelled field,
        which is much more reliable than positional child search (Java's
        method_declaration has return-type as first identifier, which v1's
        positional approach mis-extracts).
        """
        # 0. Field-name lookup (most reliable for modern grammars)
        try:
            named = node.child_by_field_name("name")
            if named is not None:
                return named.text.decode("utf-8", errors="replace")
        except Exception:
            pass

        # 1. Direct name/identifier child
        for child in node.children:
            if child.type in _NAME_NODE_TYPES:
                return child.text.decode("utf-8", errors="replace")
        # 2. After `func`/`fun`/`function` keyword (Swift, Kotlin)
        children = list(node.children)
        for i, child in enumerate(children):
            if child.type in ("func", "fun", "function") and i + 1 < len(children):
                nxt = children[i + 1]
                if nxt.type in _NAME_NODE_TYPES:
                    return nxt.text.decode("utf-8", errors="replace")
        # 3. C/C++ function_declarator
        for child in node.children:
            if child.type == "function_declarator":
                for sub in child.children:
                    if sub.type in ("identifier", "field_identifier",
                                    "destructor_name", "qualified_identifier"):
                        return sub.text.decode("utf-8", errors="replace")
        # 4. C# / Java declarator wrapping
        for child in node.children:
            if child.type == "declarator":
                for sub in child.children:
                    if sub.type in ("identifier", "name"):
                        return sub.text.decode("utf-8", errors="replace")
        # 5. Julia signature > typed_expression > call_expression
        for child in node.children:
            if child.type == "signature":
                for sub in child.children:
                    if sub.type == "typed_expression":
                        for sub2 in sub.children:
                            if sub2.type == "call_expression" and sub2.children:
                                return sub2.children[0].text.decode("utf-8", errors="replace")
                            if sub2.type in _NAME_NODE_TYPES:
                                return sub2.text.decode("utf-8", errors="replace")
                    elif sub.type in _NAME_NODE_TYPES:
                        return sub.text.decode("utf-8", errors="replace")
        # 6. Anonymous (arrow functions, callbacks, etc.)
        return None

    @staticmethod
    def _span(node) -> int:
        return node.end_byte - node.start_byte

    def _evict_if_needed(self):
        if len(self._cache) >= self.cache_size:
            self._cache.pop(next(iter(self._cache)))


# Module-level shared instance — safe to reuse across the process
_SHARED: GenericTreeSitter | None = None


def shared() -> GenericTreeSitter:
    global _SHARED
    if _SHARED is None:
        _SHARED = GenericTreeSitter()
    return _SHARED
