"""cargo clippy subprocess wrapper — Rust linter.

clippy is the standard Rust linter, distributed with the Rust toolchain
(rustup). It has 600+ lints covering correctness, complexity, performance,
style, and pedantic suggestions. The Rust community treats clippy roughly
as the C# / Java communities treat their official analyzers.

The wrapper:
- Locates cargo (Rust toolchain) and verifies clippy component is installed
- Runs `cargo clippy --message-format=json` against a target directory
- Parses the line-delimited JSON output (NOT a single JSON blob like bandit)
- Converts each compiler message of kind "diagnostic" to a Finding
- Maps clippy's severity (warning / error) + lint name → revio Severity/Category

clippy requires a Cargo project (Cargo.toml). For non-Cargo Rust files,
this wrapper returns an empty result with a warning — that's OK, the agent
can still read those via universal tools.

Graceful absence: if cargo isn't installed, `ClippyNotInstalledError` is
raised at construction. Callers (typically ToolContext) catch it and mark
clippy as unavailable for the session.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from ...output.models import Evidence, Finding, ReviewCategory, Severity


logger = logging.getLogger(__name__)


# --- Errors ------------------------------------------------------------------


class ClippyNotInstalledError(RuntimeError):
    """cargo binary not found, or clippy component not installed.

    Install with:
        curl https://sh.rustup.rs -sSf | sh        # rustup + cargo
        rustup component add clippy                 # clippy component
    """


class ClippyExecutionError(RuntimeError):
    """clippy ran but produced unusable output."""


# --- Models ------------------------------------------------------------------


class ClippySpan(BaseModel):
    file_name: str = ""
    line_start: int = 0
    line_end: int = 0
    column_start: int = 0
    column_end: int = 0
    is_primary: bool = False


class ClippyCode(BaseModel):
    code: str = ""              # e.g. "clippy::needless_borrow"
    explanation: str | None = None


class ClippyDiagnostic(BaseModel):
    """One compiler-message of kind diagnostic from clippy."""

    message: str = ""
    code: ClippyCode = Field(default_factory=ClippyCode)
    level: str = "warning"      # warning | error | note | help | ...
    spans: list[ClippySpan] = Field(default_factory=list)
    rendered: str | None = None
    children: list[dict] = Field(default_factory=list)

    @property
    def primary_span(self) -> ClippySpan | None:
        for s in self.spans:
            if s.is_primary:
                return s
        return self.spans[0] if self.spans else None

    @property
    def lint_name(self) -> str:
        """Extract the bare lint name from clippy's code field.

        e.g. "clippy::needless_borrow"  →  "needless_borrow"
             "unused_variables"          →  "unused_variables"
        """
        code = self.code.code
        if "::" in code:
            return code.split("::", 1)[1]
        return code


# --- Severity mapping --------------------------------------------------------


_SEVERITY_MAP = {
    "error":   Severity.ERROR,
    "warning": Severity.WARNING,
    "note":    Severity.INFO,
    "help":    Severity.INFO,
}


# Categories by lint name keyword (rough heuristic; clippy has 600+ lints).
_CATEGORY_KEYWORDS = {
    ReviewCategory.SECURITY: (
        "unsafe", "transmute", "raw_pointer", "ptr_eq", "expect_used", "unwrap_used",
        "indexing_slicing", "integer_arithmetic",
    ),
    ReviewCategory.POTENTIAL_BUG: (
        "needless", "match_same_arms", "panic", "unreachable", "while_immutable",
        "deref_addrof", "uninit_assumed_init", "self_assignment", "wrong_self_convention",
        "useless", "result_unit_err", "missing_panics", "unimplemented",
    ),
    ReviewCategory.PERFORMANCE: (
        "perf", "redundant_clone", "inefficient", "large_enum_variant",
        "slow_vector_initialization", "unnecessary_to_owned",
    ),
    ReviewCategory.REDUNDANCY: (
        "unused", "dead_code", "duplicate", "no_effect",
    ),
    ReviewCategory.READABILITY: (
        "too_many_arguments", "cognitive_complexity", "too_many_lines",
    ),
}


def _map_severity(diag: ClippyDiagnostic) -> Severity:
    base = _SEVERITY_MAP.get(diag.level, Severity.WARNING)
    name = diag.lint_name
    # Specific lints with elevated severity
    if name in {"correctness", "perf"} and base == Severity.WARNING:
        return Severity.ERROR
    return base


def _map_category(diag: ClippyDiagnostic) -> ReviewCategory:
    name = diag.lint_name.lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(kw in name for kw in kws):
            return cat
    return ReviewCategory.CODE_STYLE


# --- Runner ------------------------------------------------------------------


class ClippyRunner:
    """Run cargo clippy and return structured diagnostics + Findings."""

    DEFAULT_TIMEOUT_SECONDS = 300  # clippy can take a while on big crates

    def __init__(self, cargo: str | None = None, timeout: int | None = None):
        self.cargo = cargo or self._locate_cargo()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    # ---- Public API ----

    def scan(self, target_dir: Path | str) -> list[ClippyDiagnostic]:
        """Run clippy on a Cargo project directory; return parsed diagnostics."""
        target = Path(target_dir).resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"target dir does not exist: {target}")

        # Must have a Cargo.toml somewhere; clippy walks up to find one.
        if not (target / "Cargo.toml").is_file():
            logger.info("clippy: no Cargo.toml at %s — clippy needs a Cargo project", target)
            return []

        cmd = [
            self.cargo, "clippy",
            "--message-format=json",
            "--quiet",
            "--all-targets",
            "--",
            "-W", "clippy::all",
        ]
        logger.debug("running: %s (cwd=%s)", " ".join(cmd), target)

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ClippyExecutionError(
                f"clippy timed out after {self.timeout}s on {target}"
            ) from e

        # Each line is a JSON object. Filter to compiler-message kinds.
        diagnostics: list[ClippyDiagnostic] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("reason") != "compiler-message":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            # Skip the cargo build-summary noise (level "failure-note", "error: aborting", etc.)
            if not msg.get("spans"):
                continue
            try:
                diagnostics.append(ClippyDiagnostic.model_validate(msg))
            except Exception as e:
                logger.debug("clippy: skipping malformed diagnostic: %s", e)
                continue
        return diagnostics

    def scan_to_findings(
        self,
        target_dir: Path | str,
        *,
        repo_root: Path | str | None = None,
    ) -> list[Finding]:
        diagnostics = self.scan(target_dir)
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(d, root) for d in diagnostics if d.primary_span]

    # ---- Internals ----

    @staticmethod
    def _locate_cargo() -> str:
        env_path = os.environ.get("REVIO_CARGO_BIN")
        if env_path and Path(env_path).is_file():
            return env_path

        # cargo lives in ~/.cargo/bin/cargo on most systems
        home_cargo = Path.home() / ".cargo" / "bin" / "cargo"
        if home_cargo.is_file():
            return str(home_cargo)

        which = shutil.which("cargo")
        if which:
            return which

        raise ClippyNotInstalledError(
            "cargo not found. Install rustup + clippy:\n"
            "  curl https://sh.rustup.rs -sSf | sh\n"
            "  rustup component add clippy\n"
            "(Or set REVIO_CARGO_BIN to the cargo binary path)"
        )

    @staticmethod
    def _to_finding(diag: ClippyDiagnostic, repo_root: Path | None) -> Finding:
        span = diag.primary_span
        if span is None:
            # Caller filters these out, but guard for safety.
            raise ValueError("clippy diagnostic has no primary_span")

        file_path = span.file_name
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass

        evidence: list[Evidence] = [
            Evidence(
                kind="static_rule",
                summary=f"clippy {diag.lint_name}: {diag.message}",
                source=f"clippy::{diag.lint_name}",
            )
        ]
        if diag.rendered:
            evidence.append(Evidence(
                kind="code_excerpt",
                summary=diag.rendered.split("\n", 1)[0][:200],
                detail=diag.rendered,
                source=f"{file_path}:{span.line_start}",
            ))

        title = diag.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=span.line_start,
            line_end=span.line_end if span.line_end != span.line_start else None,
            severity=_map_severity(diag),
            category=_map_category(diag),
            title=title,
            hypothesis=f"clippy lint '{diag.lint_name}' triggered: {diag.message}",
            evidence=evidence,
            confidence=0.95,
            verified=True,
            suggestion=(
                f"See `cargo clippy --help` or "
                f"https://rust-lang.github.io/rust-clippy/master/#{diag.lint_name}"
            ),
            detected_by="static",
        )
