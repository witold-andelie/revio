"""JavaScript / TypeScript symbol graph.

Walks a project, parses every JS/TS file via JSTreeSitter, and builds a
cross-file index of:
- which file exports which symbols
- which file imports what from where (with module resolution)
- which files import from each file (the reverse — used to find callers)

This is the data structure that powers:
- get_call_sites tool (M2)
- find_similar_functions tool (M2 → dedup)
- audit mode's "trace user input → DB" investigations (M3)

For M2 scope:
- relative imports resolved against file's directory
- tsconfig `paths` aliases NOT yet supported (M3 enhancement)
- bare imports ("react", "axios") marked external — not resolved
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .treesitter_js import (
    ExportInfo,
    FunctionInfo,
    ImportInfo,
    JSTreeSitter,
)


logger = logging.getLogger(__name__)


# File extensions we attempt module resolution against (in priority order)
_RESOLUTION_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Directories we never walk into
_IGNORE_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", ".nuxt",
    "coverage", "__pycache__", ".cache", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "out",
}


# --- Models -------------------------------------------------------------------


@dataclass
class FileSymbols:
    """Symbol table for one JS/TS file."""

    path: Path
    relative_path: str
    functions: list[FunctionInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    exports: list[ExportInfo] = field(default_factory=list)


@dataclass
class ResolvedImport:
    """One import resolved to a file path (or marked external)."""

    importer: Path            # file doing the importing
    source: str               # raw import specifier
    resolved: Path | None     # resolved file path (None if external)
    is_external: bool         # True for bare imports ("react", "axios")
    line: int


# --- SymbolGraph --------------------------------------------------------------


class SymbolGraph:
    """Cross-file symbol index for a JS/TS project."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.parser = JSTreeSitter()

        # Primary indexes
        self.files: dict[Path, FileSymbols] = {}
        # symbol name → list of (file, ExportInfo)
        self.exports_by_name: dict[str, list[tuple[Path, ExportInfo]]] = defaultdict(list)
        # imported file → list of importing (file, ImportInfo)
        self.importers_by_file: dict[Path, list[tuple[Path, ImportInfo]]] = defaultdict(list)
        # Resolved imports per file (importer → [ResolvedImport])
        self.resolved_imports: dict[Path, list[ResolvedImport]] = defaultdict(list)

    # ---- Building ----

    @classmethod
    def build(cls, repo_root: Path | str, max_files: int = 5000) -> "SymbolGraph":
        """Walk repo_root, parse every JS/TS file, build the index."""
        graph = cls(Path(repo_root))
        files = list(graph._discover_files(max_files))
        logger.info("symbol_graph: discovered %d JS/TS files", len(files))

        # Pass 1: parse each file, collect FileSymbols
        for path in files:
            graph._index_file(path)

        # Pass 2: resolve imports
        for fs in graph.files.values():
            for imp in fs.imports:
                resolved = graph._resolve_import(fs.path, imp.source)
                ri = ResolvedImport(
                    importer=fs.path,
                    source=imp.source,
                    resolved=resolved,
                    is_external=resolved is None and not imp.source.startswith((".", "/")),
                    line=imp.line,
                )
                graph.resolved_imports[fs.path].append(ri)
                if resolved is not None:
                    graph.importers_by_file[resolved].append((fs.path, imp))

        return graph

    def _discover_files(self, max_files: int):
        count = 0
        for p in self.repo_root.rglob("*"):
            if count >= max_files:
                break
            if not p.is_file():
                continue
            if p.suffix.lower() not in _RESOLUTION_EXTS:
                continue
            # Skip if any parent is in ignore set
            rel_parts = p.relative_to(self.repo_root).parts
            if any(part in _IGNORE_DIRS for part in rel_parts[:-1]):
                continue
            count += 1
            yield p

    def _index_file(self, path: Path) -> None:
        try:
            functions = self.parser.list_functions(path)
            imports = self.parser.list_imports(path)
            exports = self.parser.list_exports(path)
        except Exception as e:
            logger.debug("symbol_graph: skip %s (%s)", path, e)
            return

        try:
            rel = str(path.relative_to(self.repo_root))
        except ValueError:
            rel = str(path)

        fs = FileSymbols(
            path=path,
            relative_path=rel,
            functions=functions,
            imports=imports,
            exports=exports,
        )
        self.files[path] = fs

        # Index exports by name
        for ex in exports:
            for name in ex.exported_names:
                self.exports_by_name[name].append((path, ex))
            if ex.is_default and not ex.exported_names:
                # Default export with no explicit name — index by "<default>" + file stem
                self.exports_by_name[f"default:{path.stem}"].append((path, ex))

    # ---- Module resolution ----

    def _resolve_import(self, importer: Path, source: str) -> Path | None:
        """Resolve a module specifier relative to importer's directory.

        Rules (M2 subset):
        - If source starts with "./" or "../" or "/", treat as relative/absolute
        - Otherwise, treat as external (return None)
        - Try extensions in _RESOLUTION_EXTS
        - Try /index.* for directories
        """
        if not source.startswith((".", "/")):
            return None  # bare module — external

        # Build base path
        if source.startswith("/"):
            base = self.repo_root / source.lstrip("/")
        else:
            base = (importer.parent / source).resolve()

        # 1. Try base as-is (if it already has an extension)
        if base.suffix.lower() in _RESOLUTION_EXTS and base.is_file():
            return base

        # 2. Try base + ext
        for ext in _RESOLUTION_EXTS:
            candidate = base.with_suffix(ext) if base.suffix else Path(str(base) + ext)
            if candidate.is_file():
                return candidate

        # 3. Try base/index.*
        if base.is_dir():
            for ext in _RESOLUTION_EXTS:
                candidate = base / f"index{ext}"
                if candidate.is_file():
                    return candidate

        # 4. If base has a different extension (e.g., .css), accept it as-is
        if base.is_file():
            return base

        return None

    # ---- Query API ----

    def get_definitions(self, name: str) -> list[tuple[Path, ExportInfo]]:
        """All files exporting a symbol with this name."""
        return list(self.exports_by_name.get(name, []))

    def get_importers(self, file_path: Path | str) -> list[tuple[Path, ImportInfo]]:
        """All files that import from a given file."""
        return list(self.importers_by_file.get(Path(file_path).resolve(), []))

    def get_file_symbols(self, file_path: Path | str) -> FileSymbols | None:
        return self.files.get(Path(file_path).resolve())

    def list_files(self) -> list[Path]:
        return list(self.files.keys())

    def stats(self) -> dict[str, int]:
        total_functions = sum(len(fs.functions) for fs in self.files.values())
        total_imports = sum(len(fs.imports) for fs in self.files.values())
        total_exports = sum(len(fs.exports) for fs in self.files.values())
        external_imports = sum(
            1 for ri_list in self.resolved_imports.values() for ri in ri_list if ri.is_external
        )
        unresolved = sum(
            1 for ri_list in self.resolved_imports.values()
            for ri in ri_list if ri.resolved is None and not ri.is_external
        )
        return {
            "files": len(self.files),
            "functions": total_functions,
            "imports": total_imports,
            "exports": total_exports,
            "external_imports": external_imports,
            "unresolved_imports": unresolved,
        }
