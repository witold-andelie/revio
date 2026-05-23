"""Python profile runtime — keeps tree-sitter / bandit imports lazy."""

from __future__ import annotations


def make_python_tools_for_profile(ctx) -> list:
    from ..agent.python_tools import make_python_tools

    return make_python_tools(ctx)
