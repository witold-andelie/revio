"""Tree-sitter language grammar loading and management.

Ported from v1 (src/parsing/language_support.py). Adapted for revio:
- Single shared instance via module-level cache (not per-call)
- Graceful per-language fallback when a grammar package is missing
- 18 languages confirmed working with current PyPI packages

Languages supported (all best-effort — missing grammars are skipped silently):
    python, javascript, typescript, c_sharp, java, go, rust, c, cpp,
    php, ruby, lua, sql, julia, scala, kotlin, swift, shell

Excluded from v1 (no stable PyPI package as of mid-2025):
    matlab, solidity, verilog, zig, objective_c
"""

from __future__ import annotations

import logging
from typing import Callable, Optional


logger = logging.getLogger(__name__)


# Each loader returns a tree_sitter.Language or None on failure.
_Loader = Callable[[], "object | None"]


def _load_python():
    import tree_sitter
    import tree_sitter_python
    return tree_sitter.Language(tree_sitter_python.language())


def _load_javascript():
    import tree_sitter
    import tree_sitter_javascript
    return tree_sitter.Language(tree_sitter_javascript.language())


def _load_typescript():
    import tree_sitter
    import tree_sitter_typescript
    return tree_sitter.Language(tree_sitter_typescript.language_typescript())


def _load_tsx():
    import tree_sitter
    import tree_sitter_typescript
    return tree_sitter.Language(tree_sitter_typescript.language_tsx())


def _load_c_sharp():
    import tree_sitter
    import tree_sitter_c_sharp
    return tree_sitter.Language(tree_sitter_c_sharp.language())


def _load_java():
    import tree_sitter
    import tree_sitter_java
    return tree_sitter.Language(tree_sitter_java.language())


def _load_go():
    import tree_sitter
    import tree_sitter_go
    return tree_sitter.Language(tree_sitter_go.language())


def _load_rust():
    import tree_sitter
    import tree_sitter_rust
    return tree_sitter.Language(tree_sitter_rust.language())


def _load_c():
    import tree_sitter
    import tree_sitter_c
    return tree_sitter.Language(tree_sitter_c.language())


def _load_cpp():
    import tree_sitter
    import tree_sitter_cpp
    return tree_sitter.Language(tree_sitter_cpp.language())


def _load_php():
    import tree_sitter
    import tree_sitter_php
    return tree_sitter.Language(tree_sitter_php.language_php())


def _load_ruby():
    import tree_sitter
    import tree_sitter_ruby
    return tree_sitter.Language(tree_sitter_ruby.language())


def _load_lua():
    import tree_sitter
    import tree_sitter_lua
    return tree_sitter.Language(tree_sitter_lua.language())


def _load_sql():
    import tree_sitter
    import tree_sitter_sql
    return tree_sitter.Language(tree_sitter_sql.language())


def _load_julia():
    import tree_sitter
    import tree_sitter_julia
    return tree_sitter.Language(tree_sitter_julia.language())


def _load_scala():
    import tree_sitter
    import tree_sitter_scala
    return tree_sitter.Language(tree_sitter_scala.language())


def _load_kotlin():
    import tree_sitter
    import tree_sitter_kotlin
    return tree_sitter.Language(tree_sitter_kotlin.language())


def _load_swift():
    import tree_sitter
    import tree_sitter_swift
    return tree_sitter.Language(tree_sitter_swift.language())


def _load_shell():
    import tree_sitter
    import tree_sitter_bash
    return tree_sitter.Language(tree_sitter_bash.language())


# Optional / experimental — present in v1 but no stable PyPI package today.
def _load_matlab():
    import tree_sitter
    import tree_sitter_matlab
    return tree_sitter.Language(tree_sitter_matlab.language())


def _load_solidity():
    import tree_sitter
    import tree_sitter_solidity
    return tree_sitter.Language(tree_sitter_solidity.language())


def _load_verilog():
    import tree_sitter
    import tree_sitter_verilog
    return tree_sitter.Language(tree_sitter_verilog.language())


def _load_zig():
    import tree_sitter
    import tree_sitter_zig
    return tree_sitter.Language(tree_sitter_zig.language())


def _load_objective_c():
    import tree_sitter
    import tree_sitter_objc
    return tree_sitter.Language(tree_sitter_objc.language())


_LOADERS: dict[str, _Loader] = {
    "python": _load_python,
    "javascript": _load_javascript,
    "typescript": _load_typescript,
    "tsx": _load_tsx,
    "c_sharp": _load_c_sharp,
    "java": _load_java,
    "go": _load_go,
    "rust": _load_rust,
    "c": _load_c,
    "cpp": _load_cpp,
    "php": _load_php,
    "ruby": _load_ruby,
    "lua": _load_lua,
    "sql": _load_sql,
    "julia": _load_julia,
    "scala": _load_scala,
    "kotlin": _load_kotlin,
    "swift": _load_swift,
    "shell": _load_shell,
    # Experimental — only attempted if the package happens to be installed.
    "matlab": _load_matlab,
    "solidity": _load_solidity,
    "verilog": _load_verilog,
    "zig": _load_zig,
    "objective_c": _load_objective_c,
}


class LanguageSupport:
    """Lazy-loading Tree-sitter language registry.

    Only loads grammars on first request. Failures are cached so we don't
    keep paying the import cost for an unavailable language.
    """

    def __init__(self):
        self._parsers: dict[str, "object"] = {}
        self._languages: dict[str, "object"] = {}
        self._failed: set[str] = set()

    def get_parser(self, language: str):
        """Get a Tree-sitter Parser for `language`, or None if unsupported."""
        if language in self._parsers:
            return self._parsers[language]
        if language in self._failed:
            return None
        lang_obj = self.get_language(language)
        if lang_obj is None:
            return None
        try:
            import tree_sitter

            parser = tree_sitter.Parser(lang_obj)
        except Exception as e:
            logger.debug("Parser init failed for %s: %s", language, e)
            self._failed.add(language)
            return None
        self._parsers[language] = parser
        return parser

    def get_language(self, language: str):
        """Get a Tree-sitter Language object for `language`, or None."""
        if language in self._languages:
            return self._languages[language]
        if language in self._failed:
            return None
        loader = _LOADERS.get(language)
        if loader is None:
            self._failed.add(language)
            return None
        try:
            lang_obj = loader()
        except Exception as e:
            logger.debug("Grammar load failed for %s: %s", language, e)
            self._failed.add(language)
            return None
        if lang_obj is None:
            self._failed.add(language)
            return None
        self._languages[language] = lang_obj
        return lang_obj

    def is_supported(self, language: str) -> bool:
        return self.get_language(language) is not None

    def supported_languages(self) -> list[str]:
        """List of languages whose grammar successfully loaded (so far)."""
        # Try-load anything not yet attempted
        for lang in _LOADERS:
            if lang not in self._languages and lang not in self._failed:
                self.get_language(lang)
        return sorted(self._languages.keys())


# Module-level shared singleton — Tree-sitter Language objects are immutable
# and safe to share across the whole process.
_SHARED: LanguageSupport | None = None


def shared() -> LanguageSupport:
    global _SHARED
    if _SHARED is None:
        _SHARED = LanguageSupport()
    return _SHARED
