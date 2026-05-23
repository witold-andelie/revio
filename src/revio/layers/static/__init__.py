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
from .cppcheck import (
    CppCheckError,
    CppcheckNotInstalledError,
    CppcheckRunner,
)
from .golangci_lint import (
    GolangCILintNotInstalledError,
    GolangCILintRunner,
    GoLintIssue,
)
from .oxlint import (
    OxlintDiagnostic,
    OxlintNotInstalledError,
    OxlintResult,
    OxlintRunner,
)
from .spotbugs import (
    SpotBugsBug,
    SpotBugsNotInstalledError,
    SpotBugsRunner,
)

__all__ = [
    # JS
    "OxlintDiagnostic", "OxlintNotInstalledError", "OxlintResult", "OxlintRunner",
    # Python
    "BanditNotInstalledError", "BanditReport", "BanditResult", "BanditRunner",
    # Rust
    "ClippyDiagnostic", "ClippyNotInstalledError", "ClippyRunner",
    # Java
    "SpotBugsBug", "SpotBugsNotInstalledError", "SpotBugsRunner",
    # Go
    "GolangCILintNotInstalledError", "GolangCILintRunner", "GoLintIssue",
    # C/C++
    "CppCheckError", "CppcheckNotInstalledError", "CppcheckRunner",
]
