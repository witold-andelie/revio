"""Generic profile runtime — lazy AST tool imports."""

from __future__ import annotations


def make_generic_tools_for_profile(ctx) -> list:
    from ..agent.generic_tools import make_generic_ast_tools

    return list(make_generic_ast_tools(ctx))
