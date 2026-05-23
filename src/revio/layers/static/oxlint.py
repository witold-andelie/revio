"""oxlint subprocess wrapper.

oxlint is the Oxc-project Rust-based JavaScript/TypeScript linter. We shell
out to its CLI in `--format=json` mode so we get structured output without
needing a Python binding.

The wrapper:
- Locates the oxlint binary (env var > PATH > npm global > clear error)
- Runs against a file or directory with a timeout
- Parses the JSON output into typed models
- Converts each diagnostic into our generic Finding model
- Maps a select set of rules to higher severity (security-relevant ones)

oxlint exits with code 0 even when warnings are present (use --deny-warnings
to flip that). We ignore exit code and rely solely on the JSON.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from ...output.models import (
    Evidence,
    Finding,
    ReviewCategory,
    Severity,
)


logger = logging.getLogger(__name__)


# --- Errors -------------------------------------------------------------------


class OxlintNotInstalledError(RuntimeError):
    """Raised when the oxlint binary cannot be located.

    Install via:  npm install -g oxlint
    """


class OxlintExecutionError(RuntimeError):
    """Raised when oxlint runs but its output cannot be parsed."""


# --- Models -------------------------------------------------------------------


class OxlintSpan(BaseModel):
    """A source range reported by oxlint."""

    offset: int = 0
    length: int = 0
    line: int = 0
    column: int = 0


class OxlintLabel(BaseModel):
    """A labelled span inside a diagnostic."""

    label: str | None = None
    span: OxlintSpan = Field(default_factory=OxlintSpan)


class OxlintDiagnostic(BaseModel):
    """One issue reported by oxlint."""

    message: str = ""
    code: str = ""              # e.g. "eslint(no-eval)"
    severity: str = "warning"   # oxlint reports "warning" or "error"
    help: str | None = None
    filename: str = ""
    labels: list[OxlintLabel] = Field(default_factory=list)
    url: str | None = None

    @property
    def rule_id(self) -> str:
        """Extract the bare rule id from oxlint's `code` field.

        e.g. "eslint(no-eval)"        → "no-eval"
             "typescript(no-explicit-any)" → "no-explicit-any"
        """
        code = self.code
        if "(" in code and code.endswith(")"):
            return code[code.index("(") + 1 : -1]
        return code

    @property
    def primary_line(self) -> int:
        if self.labels:
            return self.labels[0].span.line
        return 0

    @property
    def primary_column(self) -> int:
        if self.labels:
            return self.labels[0].span.column
        return 0


class OxlintResult(BaseModel):
    """Top-level oxlint JSON output."""

    diagnostics: list[OxlintDiagnostic] = Field(default_factory=list)
    number_of_files: int = 0
    number_of_rules: int = 0
    threads_count: int = 0
    start_time: float = 0.0


# --- Rule → severity mapping --------------------------------------------------


# Rules where the default WARN severity should be escalated. The bulk of the
# list is opinionated security/correctness. Anything not listed keeps its
# oxlint-reported severity (warning by default).
_RULE_SEVERITY_OVERRIDES: dict[str, Severity] = {
    # Security
    "no-eval":                       Severity.CRITICAL,
    "no-implied-eval":               Severity.CRITICAL,
    "no-new-func":                   Severity.CRITICAL,
    "no-script-url":                 Severity.ERROR,
    "no-prototype-builtins":         Severity.ERROR,
    "no-extend-native":              Severity.ERROR,
    "no-proto":                      Severity.ERROR,
    "no-iterator":                   Severity.ERROR,
    "no-with":                       Severity.ERROR,
    # React/JSX security
    "react/no-danger":               Severity.ERROR,
    "react/no-danger-with-children": Severity.CRITICAL,
    "react/jsx-no-target-blank":     Severity.ERROR,
    # Correctness
    "no-self-compare":               Severity.ERROR,
    "no-unsafe-finally":             Severity.ERROR,
    "no-unsafe-negation":            Severity.ERROR,
    "no-unreachable":                Severity.ERROR,
    "no-unreachable-loop":           Severity.ERROR,
    "no-unmodified-loop-condition":  Severity.ERROR,
    "no-dupe-keys":                  Severity.ERROR,
    "no-dupe-args":                  Severity.ERROR,
    "no-dupe-class-members":         Severity.ERROR,
    "no-const-assign":               Severity.CRITICAL,
    "use-isnan":                     Severity.ERROR,
    "valid-typeof":                  Severity.ERROR,
    "for-direction":                 Severity.ERROR,
    "getter-return":                 Severity.ERROR,
    # Promise / async hazards
    "no-async-promise-executor":     Severity.ERROR,
    "no-promise-executor-return":    Severity.ERROR,
    "require-await":                 Severity.WARNING,
}


# Categorisation by rule prefix / name
_SECURITY_KEYWORDS = (
    "no-eval", "no-implied-eval", "no-new-func", "no-script-url",
    "no-prototype-builtins", "no-extend-native", "no-proto", "no-iterator",
    "no-with", "no-danger", "no-target-blank", "no-unsanitized",
)

_BUG_KEYWORDS = (
    "no-self-compare", "no-unsafe", "no-unreachable", "no-dupe", "no-const-assign",
    "use-isnan", "valid-typeof", "for-direction", "getter-return",
    "no-promise-executor-return", "no-async-promise-executor",
    "no-unmodified-loop-condition",
)


def _category_for(rule_id: str) -> ReviewCategory:
    if any(k in rule_id for k in _SECURITY_KEYWORDS):
        return ReviewCategory.SECURITY
    if any(k in rule_id for k in _BUG_KEYWORDS):
        return ReviewCategory.POTENTIAL_BUG
    if "no-unused" in rule_id or "unused" in rule_id:
        return ReviewCategory.REDUNDANCY
    if "perf" in rule_id or "performance" in rule_id:
        return ReviewCategory.PERFORMANCE
    return ReviewCategory.CODE_STYLE


def _severity_for(diag: OxlintDiagnostic) -> Severity:
    # Rule-specific override
    override = _RULE_SEVERITY_OVERRIDES.get(diag.rule_id)
    if override:
        return override
    # Otherwise use oxlint's own severity
    if diag.severity == "error":
        return Severity.ERROR
    return Severity.WARNING


# --- Runner -------------------------------------------------------------------


class OxlintRunner:
    """Run oxlint on a file or directory and return structured diagnostics."""

    DEFAULT_TIMEOUT_SECONDS = 60

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    # ---- Public API ----

    def lint(self, target: Path | str) -> OxlintResult:
        """Run oxlint on a file or directory, return parsed result."""
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        cmd = [self.binary, "--format=json", str(target)]
        logger.debug("running: %s", " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise OxlintExecutionError(
                f"oxlint timed out after {self.timeout}s on {target}"
            ) from e

        if not proc.stdout.strip():
            # No JSON emitted — oxlint may have errored out structurally
            stderr = (proc.stderr or "").strip()
            raise OxlintExecutionError(
                f"oxlint produced no JSON output (exit={proc.returncode}, stderr={stderr[:200]})"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise OxlintExecutionError(
                f"oxlint emitted invalid JSON: {e}; first 200 chars: {proc.stdout[:200]!r}"
            ) from e

        return OxlintResult.model_validate(data)

    def lint_to_findings(
        self,
        target: Path | str,
        *,
        repo_root: Path | str | None = None,
    ) -> list[Finding]:
        """Convenience: run lint and convert each diagnostic to a Finding.

        If repo_root is provided, finding file_paths are made relative to it.
        """
        result = self.lint(target)
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(d, root) for d in result.diagnostics]

    # ---- Internals ----

    @staticmethod
    def _locate_binary() -> str:
        """Find oxlint via env var → PATH → npm global → fail loudly."""
        env_path = os.environ.get("REVIO_OXLINT_BIN")
        if env_path and Path(env_path).is_file():
            return env_path

        which = shutil.which("oxlint")
        if which:
            return which

        # Try npm global install location
        npm_root = shutil.which("npm")
        if npm_root:
            try:
                out = subprocess.run(
                    [npm_root, "root", "-g"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                npm_global = out.stdout.strip()
                if npm_global:
                    candidate = Path(npm_global).parent / "bin" / "oxlint"
                    if candidate.is_file():
                        return str(candidate)
            except (subprocess.SubprocessError, OSError):
                pass

        raise OxlintNotInstalledError(
            "oxlint binary not found. Install with:  npm install -g oxlint  "
            "(or set REVIO_OXLINT_BIN to its path)"
        )

    @staticmethod
    def _to_finding(diag: OxlintDiagnostic, repo_root: Path | None) -> Finding:
        # Compute relative path
        file_path = diag.filename
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass  # Not under repo_root; keep absolute

        line = diag.primary_line or 1

        # Evidence: rule + message + any labels
        evidence: list[Evidence] = [
            Evidence(
                kind="static_rule",
                summary=f"oxlint rule {diag.rule_id}: {diag.message}",
                source=diag.code,
            )
        ]
        for label in diag.labels:
            if label.label:
                evidence.append(
                    Evidence(
                        kind="code_excerpt",
                        summary=label.label,
                        source=f"{file_path}:{label.span.line}",
                    )
                )

        # Suggestion = oxlint's `help` field
        suggestion = diag.help

        # Build title (max ~10 words)
        title = diag.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=line,
            line_end=None,
            severity=_severity_for(diag),
            category=_category_for(diag.rule_id),
            title=title,
            hypothesis=f"oxlint rule '{diag.rule_id}' triggered: {diag.message}",
            evidence=evidence,
            confidence=0.95,  # oxlint is deterministic; high confidence
            verified=True,
            suggestion=suggestion,
            detected_by="static",
        )
