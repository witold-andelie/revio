"""Java profile runtime — lazy spotbugs / tree-sitter imports."""

from __future__ import annotations


def make_java_tools_for_profile(ctx) -> list:
    from ..agent.java_tools import make_java_tools

    return make_java_tools(ctx)
