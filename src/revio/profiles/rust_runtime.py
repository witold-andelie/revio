"""Rust profile runtime — lazy clippy/tree-sitter imports."""

from __future__ import annotations


def make_rust_tools_for_profile(ctx) -> list:
    from ..agent.rust_tools import make_rust_tools

    return make_rust_tools(ctx)
