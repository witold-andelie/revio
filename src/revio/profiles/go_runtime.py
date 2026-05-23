"""Go profile runtime — lazy golangci-lint / tree-sitter imports."""

from __future__ import annotations


def make_go_tools_for_profile(ctx) -> list:
    from ..agent.go_tools import make_go_tools

    return make_go_tools(ctx)
