"""C/C++ profile runtime — lazy cppcheck / tree-sitter imports."""

from __future__ import annotations


def make_cpp_tools_for_profile(ctx) -> list:
    from ..agent.cpp_tools import make_cpp_tools

    return make_cpp_tools(ctx)
