"""PLC profile runtime — lazy imports of vendor parsers + rules."""

from __future__ import annotations


def make_plc_tools_for_profile(ctx) -> list:
    from ..agent.plc_tools import make_plc_tools

    return make_plc_tools(ctx)
