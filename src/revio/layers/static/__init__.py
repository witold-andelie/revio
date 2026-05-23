"""Layer 2 — Static analysis.

Currently exposes the JS oxlint integration. PLC and Python equivalents
arrive later (M4 for PLC port, M3 for bandit/semgrep if needed).
"""

from .oxlint import (
    OxlintDiagnostic,
    OxlintNotInstalledError,
    OxlintResult,
    OxlintRunner,
)

__all__ = [
    "OxlintDiagnostic",
    "OxlintNotInstalledError",
    "OxlintResult",
    "OxlintRunner",
]
