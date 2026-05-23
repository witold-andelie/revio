"""Layer 1 — Parser.

AST + future CFG + symbol graph. JS/TS via Tree-sitter for M2.
PLC and Python wiring deferred (M4 / M3).
"""

from .treesitter_js import (
    ExportInfo,
    FunctionInfo,
    ImportInfo,
    JSTreeSitter,
    TreeSitterUnavailable,
)

__all__ = [
    "ExportInfo",
    "FunctionInfo",
    "ImportInfo",
    "JSTreeSitter",
    "TreeSitterUnavailable",
]
