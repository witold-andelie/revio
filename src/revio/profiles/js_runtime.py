"""JS profile runtime hookup — keeps heavy imports out of the profile
declaration so `load_all_profiles()` stays cheap even when JS isn't active.
"""

from __future__ import annotations


def make_js_tools_for_profile(ctx) -> list:
    """Return the JS profile's agent tools, bound to a ToolContext."""
    from ..agent.js_tools import make_js_tools

    return make_js_tools(ctx)
