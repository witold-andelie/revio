"""Profile registry and base class.

A "profile" is a per-language pack that supplies:
- Which file extensions it handles
- Which static-analysis tools to wire into Layer 2
- Optional framework-specific hints to inject into Layer 3 prompts
- Optional advanced analyzers (Layer 4)

Profiles register themselves via `@register("name")` decorator at import time.
The CLI's auto-detect picks one (or several) based on ProjectFingerprint.

This file only defines the abstract base + registry. Concrete profiles live in
revio.profiles.js / .plc / .python and are imported by `load_all_profiles()`.
"""

from __future__ import annotations

from typing import ClassVar


# --- Registry ------------------------------------------------------------------


_REGISTRY: dict[str, type["ProfileBase"]] = {}


def register(name: str):
    """Decorator: associate a profile class with a name."""

    def deco(cls: type["ProfileBase"]) -> type["ProfileBase"]:
        if name in _REGISTRY:
            raise ValueError(f"Profile name '{name}' already registered")
        cls.name = name  # type: ignore[misc]
        _REGISTRY[name] = cls
        return cls

    return deco


def get_profile(name: str) -> type["ProfileBase"] | None:
    return _REGISTRY.get(name)


def list_profiles() -> list[str]:
    return sorted(_REGISTRY.keys())


def load_all_profiles() -> None:
    """Import every profile module so they self-register.

    Called once at startup. Safe to call multiple times (decorator raises on
    duplicate, so we swallow that). New profiles just add an `import` line.
    """
    # Local imports to avoid circular deps at module-load time.
    # Each profile's import is wrapped in try/except so a missing optional
    # dependency (e.g. tree-sitter grammar package) doesn't crash startup.
    for module_name in ("js", "plc", "python", "rust", "generic"):
        try:
            __import__(f"revio.profiles.{module_name}")
        except (ImportError, ValueError):
            # ValueError catches the "already registered" raise when this
            # function is called more than once in the same process.
            pass


# --- Base class ----------------------------------------------------------------


class ProfileBase:
    """Abstract base for a language profile.

    Subclasses override class attributes for declarative metadata and (in later
    milestones) the layer factory methods to supply real analyzers.
    """

    # Set by @register at registration time
    name: ClassVar[str] = ""

    # Declarative metadata (override in subclass)
    description: ClassVar[str] = ""
    extensions: ClassVar[tuple[str, ...]] = ()
    languages: ClassVar[tuple[str, ...]] = ()  # internal language tags

    # Optional: extra deps the user needs to `pip install revio[<name>]` for
    optional_dep_group: ClassVar[str | None] = None

    # --- Layer factories ---
    # Each returns either a configured analyzer for this profile, or None.
    # M1 stubs: all return None. Real implementations come in M2/M3.

    @classmethod
    def make_parser_layer(cls):  # -> ParserLayer | None
        """Layer 1: AST + CFG + symbol graph for this language."""
        return None

    @classmethod
    def make_static_layer(cls):  # -> StaticLayer | None
        """Layer 2: lint + taint + sink/source rules."""
        return None

    @classmethod
    def make_reasoning_hints(cls) -> str:
        """Layer 3: extra context to inject into the LLM system prompt.

        E.g. "This project uses React 18 — be alert to dangerouslySetInnerHTML."
        """
        return ""

    @classmethod
    def make_advanced_layer(cls):  # -> AdvancedLayer | None
        """Layer 4: opt-in symbolic / SMT / PoC generators."""
        return None

    @classmethod
    def make_tools(cls, ctx) -> list:
        """Profile-specific agent tools (bound to a ToolContext).

        Default: no extra tools. Subclasses override to expose their
        Layer-1/Layer-2 capabilities to the LLM.
        """
        return []

    # --- Convenience ---

    @classmethod
    def handles_extension(cls, ext: str) -> bool:
        return ext.lower() in cls.extensions
