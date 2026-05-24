"""`revio analyzers` — list & install Layer-2 static analyzers.

The same letter-coded menu the install script uses, exposed as a
post-install CLI so users can add more languages without re-running
the bootstrap installer.

  revio analyzers              # status table: which letters are installed
  revio analyzers install jcs  # install JS + C/C++ + Shell
  revio analyzers install '*'  # install all
  revio analyzers menu         # interactive picker (same as installer Stage 6b)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# --- Registry ---------------------------------------------------------------

# Single mnemonic letter per analyzer; mirrors the install scripts.
# When you add a new analyzer, edit BOTH this and the install.ps1 /
# install.sh menus to keep them in sync.
@dataclass
class AnalyzerSpec:
    code: str                              # 1-char mnemonic
    label: str                             # human label
    check_cmd: str                         # the binary that means 'installed'
    brew_pkg: str | None = None            # macOS
    apt_pkg: str | None = None             # Linux (apt)
    winget_id: str | None = None           # Windows winget
    scoop_id: str | None = None            # Windows scoop
    npm_id: str | None = None              # cross-platform npm
    manual_hint: str | None = None         # human prose for hard cases
    # Sentinel for pip-into-venv installs (sqlfluff): when check_cmd is
    # this string, the runner pip-installs into revio's own venv.
    is_pip_into_venv: bool = False
    pip_pkg: str | None = None             # only used when is_pip_into_venv


REGISTRY: list[AnalyzerSpec] = [
    AnalyzerSpec("j", "JS / TypeScript    (oxlint)",        "oxlint",
                 npm_id="oxlint"),
    AnalyzerSpec("c", "C / C++            (cppcheck)",       "cppcheck",
                 brew_pkg="cppcheck", apt_pkg="cppcheck",
                 winget_id="Cppcheck.Cppcheck", scoop_id="cppcheck"),
    AnalyzerSpec("g", "Go                 (golangci-lint)",  "golangci-lint",
                 brew_pkg="golangci-lint", apt_pkg="golangci-lint",
                 winget_id="golangci-lint.golangci-lint", scoop_id="golangci-lint"),
    AnalyzerSpec("r", "Rust               (clippy)",         "cargo-clippy",
                 manual_hint="comes with rustup — run: rustup component add clippy"),
    AnalyzerSpec("a", "Java               (spotbugs)",       "spotbugs",
                 brew_pkg="spotbugs", winget_id="SpotBugs.SpotBugs",
                 manual_hint="needs JDK; download from spotbugs.github.io"),
    AnalyzerSpec("s", "Shell              (shellcheck)",     "shellcheck",
                 brew_pkg="shellcheck", apt_pkg="shellcheck",
                 winget_id="koalaman.shellcheck", scoop_id="shellcheck"),
    AnalyzerSpec("l", "Lua                (luacheck)",       "luacheck",
                 brew_pkg="luacheck", apt_pkg="lua-check", scoop_id="luacheck",
                 manual_hint="install Scoop (https://scoop.sh) then: scoop install luacheck"),
    AnalyzerSpec("q", "SQL                (sqlfluff)",       "__SQLFLUFF__",
                 is_pip_into_venv=True, pip_pkg="sqlfluff"),
    AnalyzerSpec("v", "Verilog            (verilator)",      "verilator",
                 brew_pkg="verilator", apt_pkg="verilator", scoop_id="verilator",
                 manual_hint="install Scoop then: scoop install verilator, or use WSL"),
    AnalyzerSpec("u", "Ruby               (rubocop)",        "rubocop",
                 manual_hint="install Ruby + run: gem install rubocop"),
    AnalyzerSpec("h", "PHP                (phpstan)",        "phpstan",
                 manual_hint="install PHP + Composer + composer global require phpstan/phpstan"),
    AnalyzerSpec("k", "Kotlin             (detekt)",         "detekt",
                 brew_pkg="detekt", scoop_id="detekt",
                 manual_hint="needs JDK; download detekt-cli from GitHub"),
]


# --- Detection --------------------------------------------------------------


def is_installed(spec: AnalyzerSpec) -> bool:
    """Whether the analyzer's binary is on PATH (or in revio's venv for sqlfluff)."""
    if spec.is_pip_into_venv:
        venv_python = Path(sys.executable)  # we ARE the venv python at runtime
        try:
            r = subprocess.run(
                [str(venv_python), "-m", spec.pip_pkg or spec.code, "--version"],
                capture_output=True, timeout=10, check=False,
            )
            return r.returncode == 0
        except Exception:
            return False
    return shutil.which(spec.check_cmd) is not None


def by_code(code: str) -> AnalyzerSpec | None:
    for spec in REGISTRY:
        if spec.code == code:
            return spec
    return None


def parse_letters(raw: str) -> tuple[list[AnalyzerSpec], list[str]]:
    """Parse a letter sequence into specs + list of unknown letters.

    '*' returns the full registry. Empty returns empty list. Case-insensitive,
    spaces ignored, duplicates collapsed in order of first appearance.
    """
    raw = (raw or "").strip().lower().replace(" ", "")
    if raw == "*":
        return list(REGISTRY), []
    seen: set[str] = set()
    out: list[AnalyzerSpec] = []
    unknown: list[str] = []
    for ch in raw:
        if ch in seen:
            continue
        seen.add(ch)
        spec = by_code(ch)
        if spec is not None:
            out.append(spec)
        elif ch.isalpha():
            unknown.append(ch)
    return out, unknown


# --- Installation -----------------------------------------------------------


@dataclass
class InstallOutcome:
    spec: AnalyzerSpec
    status: str                            # 'already' | 'installed' | 'failed' | 'skipped'
    via: str = ""                          # which package manager ran
    note: str = ""                         # error message or manual hint


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run_quiet(args: list[str], timeout: int = 600) -> int:
    """Run a subprocess; return exit code; suppress output."""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return r.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127


def install_one(spec: AnalyzerSpec) -> InstallOutcome:
    """Try each available package manager in priority order."""
    if is_installed(spec):
        return InstallOutcome(spec, "already")

    # 0. sqlfluff: pip-install into revio's own venv (no system PM)
    if spec.is_pip_into_venv:
        rc = _run_quiet([sys.executable, "-m", "pip", "install", spec.pip_pkg or spec.code])
        if rc == 0:
            return InstallOutcome(spec, "installed", via="pip (revio venv)")
        return InstallOutcome(spec, "failed", via="pip", note=f"rc={rc}")

    # 1. npm — works cross-platform if Node is around
    if spec.npm_id and _have("npm"):
        rc = _run_quiet(["npm", "install", "-g", spec.npm_id, "--silent"])
        if rc == 0:
            return InstallOutcome(spec, "installed", via="npm")

    # 2. Platform-specific
    system = platform.system().lower()
    if system == "darwin" and spec.brew_pkg and _have("brew"):
        rc = _run_quiet(["brew", "install", spec.brew_pkg])
        if rc == 0:
            return InstallOutcome(spec, "installed", via="brew")
    if system == "linux" and spec.apt_pkg and _have("apt-get"):
        rc = _run_quiet(["sudo", "apt-get", "install", "-y", "-qq", spec.apt_pkg])
        if rc == 0:
            return InstallOutcome(spec, "installed", via="apt")
    if system == "windows":
        if spec.winget_id and _have("winget"):
            rc = _run_quiet([
                "winget", "install", "--silent", "--id", spec.winget_id,
                "--accept-source-agreements", "--accept-package-agreements",
            ])
            if rc == 0:
                return InstallOutcome(spec, "installed", via="winget")
        if spec.scoop_id and _have("scoop"):
            rc = _run_quiet(["scoop", "install", spec.scoop_id])
            if rc == 0:
                return InstallOutcome(spec, "installed", via="scoop")

    # 3. Nothing worked
    if spec.manual_hint:
        return InstallOutcome(spec, "skipped", note=spec.manual_hint)
    return InstallOutcome(spec, "skipped", note="no compatible package manager")


# --- Console rendering ------------------------------------------------------


def status_table_rows() -> list[tuple[str, str, str]]:
    """(letter, label, status) tuples for rendering. Status: 'installed' | 'missing'."""
    rows: list[tuple[str, str, str]] = []
    for spec in REGISTRY:
        st = "installed" if is_installed(spec) else "missing"
        rows.append((spec.code, spec.label, st))
    return rows
