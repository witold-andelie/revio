"""Layer 2 — Static analysis.

Currently exposes the JS oxlint integration. PLC and Python equivalents
arrive later (M4 for PLC port, M3 for bandit/semgrep if needed).
"""

from .bandit import (
    BanditNotInstalledError,
    BanditReport,
    BanditResult,
    BanditRunner,
)
from .clippy import (
    ClippyDiagnostic,
    ClippyNotInstalledError,
    ClippyRunner,
)
from .oxlint import (
    OxlintDiagnostic,
    OxlintNotInstalledError,
    OxlintResult,
    OxlintRunner,
)

__all__ = [
    # JS
    "OxlintDiagnostic",
    "OxlintNotInstalledError",
    "OxlintResult",
    "OxlintRunner",
    # Python
    "BanditNotInstalledError",
    "BanditReport",
    "BanditResult",
    "BanditRunner",
    # Rust
    "ClippyDiagnostic",
    "ClippyNotInstalledError",
    "ClippyRunner",
]
