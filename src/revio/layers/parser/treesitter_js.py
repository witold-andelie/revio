"""JavaScript / TypeScript AST extraction via Tree-sitter.

Provides the structural fact layer that the agent's tools consume:
- get_function_at(file, line)  — find enclosing function for a line
- list_functions(file)         — all functions / methods / arrow fns
- list_imports(file)           — every import statement
- list_exports(file)           — exported symbols
- get_node_text(file, line)    — fetch source for a node range

This is M2 Layer 1. Layer 2 (oxlint) and Layer 3 (the LLM) consume these
facts via agent tools (added in task 19).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


logger = logging.getLogger(__name__)


# --- Errors -------------------------------------------------------------------


class TreeSitterUnavailable(RuntimeError):
    """Raised when tree-sitter-{javascript,typescript} can't be imported.

    Install with:  pip install -e .[js]
    """


# --- Models -------------------------------------------------------------------


class FunctionInfo(BaseModel):
    """A function / method / arrow function declaration."""

    name: str | None = None        # null for anonymous functions
    kind: str = "function"          # function | method | arrow | constructor | getter | setter
    line_start: int                 # 1-indexed
    line_end: int
    is_async: bool = False
    is_exported: bool = False
    is_default_export: bool = False
    enclosing_class: str | None = None  # class name if it's a method
    parameter_names: list[str] = []
    body: str = ""                  # full source text of the function

    @property
    def line_count(self) -> int:
        return self.line_end - self.line_start + 1


class ImportInfo(BaseModel):
    """An import statement."""

    source: str                     # module specifier, e.g. "./utils" or "react"
    line: int                       # 1-indexed
    imported_names: list[str] = []   # named imports
    default_name: str | None = None  # default import
    namespace_name: str | None = None  # * as Foo
    is_side_effect: bool = False     # `import './style.css'`
    raw: str = ""                    # original source text


class ExportInfo(BaseModel):
    """An export declaration."""

    line: int
    exported_names: list[str] = []
    is_default: bool = False
    is_re_export: bool = False
    re_export_source: str | None = None  # for `export { x } from "./y"`
    raw: str = ""


# --- Tree-sitter binding ------------------------------------------------------


def _load_languages():
    """Lazy import + load JS/TS/TSX languages. Raises if optional deps missing."""
    try:
        from tree_sitter import Language, Parser  # noqa: F401
        import tree_sitter_javascript
        import tree_sitter_typescript
    except ImportError as e:
        raise TreeSitterUnavailable(
            "tree-sitter and tree-sitter-{javascript,typescript} are required. "
            "Install with:  pip install -e '.[js]'"
        ) from e

    from tree_sitter import Language

    return {
        "javascript": Language(tree_sitter_javascript.language()),
        "typescript": Language(tree_sitter_typescript.language_typescript()),
        "tsx": Language(tree_sitter_typescript.language_tsx()),
    }


# Lazy module-level cache (one set of language objects per process)
_LANG_CACHE: dict[str, "object"] | None = None


def _get_languages():
    global _LANG_CACHE
    if _LANG_CACHE is None:
        _LANG_CACHE = _load_languages()
    return _LANG_CACHE


# --- Per-language node-type sets ----------------------------------------------


# Function-like node types
_FUNCTION_TYPES_JS = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
    "generator_function",
}

_FUNCTION_TYPES_TS = _FUNCTION_TYPES_JS | {
    "function_signature",  # interface methods (TS only)
}


_CLASS_TYPES = {"class_declaration", "class"}


# --- JSTreeSitter -------------------------------------------------------------


@dataclass
class _ParsedFile:
    tree: object              # tree_sitter.Tree
    source: bytes
    language_name: str
    mtime: float


class JSTreeSitter:
    """Parser façade with simple LRU caching by (path, mtime)."""

    # Map file extension → tree-sitter language name
    EXT_LANG = {
        ".js": "javascript",
        ".jsx": "javascript",  # tree-sitter-javascript handles JSX
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }

    def __init__(self, cache_size: int = 64):
        self.cache_size = cache_size
        self._cache: dict[Path, _ParsedFile] = {}

    # ---- Public API ----

    def parse_file(self, path: Path | str) -> Optional[_ParsedFile]:
        """Parse a file (cached). Returns None if extension unsupported."""
        path = Path(path).resolve()
        if not path.is_file():
            return None

        lang_name = self.EXT_LANG.get(path.suffix.lower())
        if lang_name is None:
            return None

        # Cache check
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        cached = self._cache.get(path)
        if cached and cached.mtime == mtime:
            return cached

        # Parse
        from tree_sitter import Parser

        langs = _get_languages()
        lang = langs[lang_name]
        parser = Parser(lang)

        try:
            source = path.read_bytes()
        except OSError:
            return None

        tree = parser.parse(source)
        parsed = _ParsedFile(tree=tree, source=source, language_name=lang_name, mtime=mtime)

        self._evict_if_needed()
        self._cache[path] = parsed
        return parsed

    def list_functions(self, path: Path | str) -> list[FunctionInfo]:
        """All function-like nodes in the file, in source order."""
        parsed = self.parse_file(path)
        if parsed is None:
            return []

        types = _FUNCTION_TYPES_TS if parsed.language_name != "javascript" else _FUNCTION_TYPES_JS

        out: list[FunctionInfo] = []
        for node, enclosing_class in self._walk_with_class(parsed.tree.root_node):
            if node.type not in types:
                continue
            info = self._function_info(node, parsed, enclosing_class)
            if info:
                out.append(info)
        return out

    def get_function_at(self, path: Path | str, line: int) -> FunctionInfo | None:
        """Find the innermost function enclosing a given 1-indexed line."""
        parsed = self.parse_file(path)
        if parsed is None:
            return None

        target = line - 1  # tree-sitter uses 0-indexed rows
        types = _FUNCTION_TYPES_TS if parsed.language_name != "javascript" else _FUNCTION_TYPES_JS

        best: tuple[object, str | None] | None = None  # (node, class)
        for node, klass in self._walk_with_class(parsed.tree.root_node):
            if node.type not in types:
                continue
            if node.start_point[0] <= target <= node.end_point[0]:
                # Prefer the innermost (smallest span)
                if best is None or self._span(node) < self._span(best[0]):
                    best = (node, klass)
        if best is None:
            return None
        return self._function_info(best[0], parsed, best[1])

    def list_imports(self, path: Path | str) -> list[ImportInfo]:
        parsed = self.parse_file(path)
        if parsed is None:
            return []

        out: list[ImportInfo] = []
        for node in self._iter(parsed.tree.root_node):
            if node.type != "import_statement":
                continue
            info = self._import_info(node, parsed.source)
            if info:
                out.append(info)
        return out

    def list_exports(self, path: Path | str) -> list[ExportInfo]:
        parsed = self.parse_file(path)
        if parsed is None:
            return []

        out: list[ExportInfo] = []
        for node in self._iter(parsed.tree.root_node):
            if node.type != "export_statement":
                continue
            info = self._export_info(node, parsed.source)
            if info:
                out.append(info)
        return out

    def get_node_text(self, parsed: _ParsedFile, node) -> str:
        return parsed.source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # ---- Internal traversal ----

    def _iter(self, node):
        """Pre-order traversal of all nodes."""
        yield node
        for child in node.children:
            yield from self._iter(child)

    def _walk_with_class(self, root):
        """Pre-order traversal yielding (node, enclosing_class_name_or_None)."""
        stack: list[tuple[object, str | None]] = [(root, None)]
        while stack:
            node, enclosing = stack.pop()
            yield node, enclosing
            # If this node is a class, push children with class context
            if node.type in _CLASS_TYPES:
                klass_name = self._class_name(node)
                for c in reversed(node.children):
                    stack.append((c, klass_name))
            else:
                for c in reversed(node.children):
                    stack.append((c, enclosing))

    def _class_name(self, node) -> str | None:
        for child in node.children:
            if child.type == "type_identifier" or child.type == "identifier":
                return child.text.decode("utf-8", errors="replace")
        return None

    @staticmethod
    def _span(node) -> int:
        return node.end_byte - node.start_byte

    # ---- Info builders ----

    def _function_info(self, node, parsed: _ParsedFile, enclosing_class: str | None) -> FunctionInfo | None:
        # Kind
        kind_map = {
            "function_declaration": "function",
            "function_expression": "function",
            "generator_function_declaration": "function",
            "generator_function": "function",
            "arrow_function": "arrow",
            "method_definition": "method",
            "function_signature": "method",
        }
        kind = kind_map.get(node.type, "function")

        # Name
        name = self._function_name(node)
        if kind == "method" and name == "constructor":
            kind = "constructor"

        # Async + getter/setter
        is_async = self._has_keyword(node, "async")
        if kind == "method":
            if self._has_keyword(node, "get"):
                kind = "getter"
            elif self._has_keyword(node, "set"):
                kind = "setter"

        # Exported?
        parent = node.parent
        is_exported = False
        is_default = False
        while parent is not None:
            if parent.type == "export_statement":
                is_exported = True
                # Default export? Look for `default` keyword child
                if any(c.type == "default" for c in parent.children):
                    is_default = True
                break
            # Don't ascend past function boundaries
            if parent.type in (_FUNCTION_TYPES_TS | {"class_body", "program"}):
                break
            parent = parent.parent

        # Parameters
        params = self._param_names(node)

        # Body
        body = self.get_node_text(parsed, node)

        return FunctionInfo(
            name=name,
            kind=kind,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_async=is_async,
            is_exported=is_exported,
            is_default_export=is_default,
            enclosing_class=enclosing_class,
            parameter_names=params,
            body=body,
        )

    def _function_name(self, node) -> str | None:
        # function_declaration / class method / etc.
        for child in node.children:
            if child.type in ("identifier", "property_identifier", "type_identifier"):
                return child.text.decode("utf-8", errors="replace")
            if child.type == "computed_property_name":
                return child.text.decode("utf-8", errors="replace")
        # Arrow functions assigned to a variable: name lives on parent's variable_declarator
        if node.type == "arrow_function":
            parent = node.parent
            if parent and parent.type == "variable_declarator":
                for c in parent.children:
                    if c.type == "identifier":
                        return c.text.decode("utf-8", errors="replace")
        return None

    def _has_keyword(self, node, keyword: str) -> bool:
        for child in node.children:
            if child.type == keyword:
                return True
        return False

    def _param_names(self, node) -> list[str]:
        # find formal_parameters child
        for child in node.children:
            if child.type == "formal_parameters":
                names: list[str] = []
                for p in child.children:
                    if p.type == "identifier":
                        names.append(p.text.decode("utf-8", errors="replace"))
                    elif p.type == "required_parameter" or p.type == "optional_parameter":
                        # TS-specific wrapper
                        for sub in p.children:
                            if sub.type == "identifier":
                                names.append(sub.text.decode("utf-8", errors="replace"))
                                break
                    elif p.type == "rest_pattern":
                        for sub in p.children:
                            if sub.type == "identifier":
                                names.append("..." + sub.text.decode("utf-8", errors="replace"))
                                break
                return names
        return []

    def _import_info(self, node, source_bytes: bytes) -> ImportInfo | None:
        raw = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        # Find module specifier (string node)
        module = ""
        for child in node.children:
            if child.type == "string":
                # Strip quotes
                text = child.text.decode("utf-8", errors="replace")
                module = text.strip("\"'`")
                break

        info = ImportInfo(source=module, line=line, raw=raw)

        # Parse the import_clause
        for child in node.children:
            if child.type != "import_clause":
                continue
            for sub in child.children:
                if sub.type == "identifier":
                    info.default_name = sub.text.decode("utf-8", errors="replace")
                elif sub.type == "namespace_import":
                    # * as Foo
                    for n in sub.children:
                        if n.type == "identifier":
                            info.namespace_name = n.text.decode("utf-8", errors="replace")
                elif sub.type == "named_imports":
                    for spec in sub.children:
                        if spec.type == "import_specifier":
                            # Take the local name (last identifier child)
                            ids = [c.text.decode("utf-8", errors="replace") for c in spec.children if c.type == "identifier"]
                            if ids:
                                info.imported_names.append(ids[-1])
            break

        # Side-effect import: no import_clause
        if not info.default_name and not info.namespace_name and not info.imported_names:
            info.is_side_effect = True

        return info

    def _export_info(self, node, source_bytes: bytes) -> ExportInfo | None:
        raw = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        info = ExportInfo(line=line, raw=raw)
        info.is_default = any(c.type == "default" for c in node.children)

        # Re-export: `export ... from "./mod"`
        has_from = any(c.type == "from" for c in node.children)
        if has_from:
            info.is_re_export = True
            for child in node.children:
                if child.type == "string":
                    info.re_export_source = child.text.decode("utf-8", errors="replace").strip("\"'`")

        # Exported names
        for child in node.children:
            if child.type == "function_declaration":
                name = self._function_name(child)
                if name:
                    info.exported_names.append(name)
            elif child.type == "class_declaration":
                name = self._class_name(child)
                if name:
                    info.exported_names.append(name)
            elif child.type == "lexical_declaration":
                # export const foo = ...
                for d in child.children:
                    if d.type == "variable_declarator":
                        for n in d.children:
                            if n.type == "identifier":
                                info.exported_names.append(n.text.decode("utf-8", errors="replace"))
                                break
            elif child.type == "export_clause":
                # export { a, b }
                for spec in child.children:
                    if spec.type == "export_specifier":
                        ids = [c.text.decode("utf-8", errors="replace") for c in spec.children if c.type == "identifier"]
                        if ids:
                            info.exported_names.append(ids[-1])

        return info

    # ---- LRU cache ----

    def _evict_if_needed(self):
        if len(self._cache) >= self.cache_size:
            # Pop oldest (Python dicts are insertion-ordered as of 3.7)
            self._cache.pop(next(iter(self._cache)))
