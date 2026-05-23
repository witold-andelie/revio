"""Profile registry — language packs.

Usage:
    from revio.profiles import load_all_profiles, get_profile, list_profiles
    load_all_profiles()
    js = get_profile("js")
"""

from .base import (
    ProfileBase,
    get_profile,
    list_profiles,
    load_all_profiles,
    register,
)

__all__ = [
    "ProfileBase",
    "get_profile",
    "list_profiles",
    "load_all_profiles",
    "register",
]
