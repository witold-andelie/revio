"""bandit subprocess wrapper — Python security linter.

bandit is the de-facto Python security analyzer. Identifies common patterns
like hardcoded passwords, pickle deserialization, shell=True subprocess
calls, weak crypto, eval, etc.

The wrapper:
- Locates the bandit binary (env var → revio's venv → PATH → npm-style fallback)
- Runs `bandit -r <target> -f json` and parses the structured output
- Converts each finding to revio's generic Finding model
- Maps bandit severity (LOW/MEDIUM/HIGH) to revio's INFO/WARNING/ERROR/CRITICAL
- Maps test_id → CWE → category (security mostly; some are convention)

bandit exits non-zero when it finds anything; we ignore exit code, rely on JSON.
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


class BanditNotInstalledError(RuntimeError):
    """bandit binary not found. Install with: pip install bandit"""


class BanditExecutionError(RuntimeError):
    """bandit ran but produced unusable output."""


# --- Models ------------------------------------------------------------------


class BanditResult(BaseModel):
    filename: str = ""
    line_number: int = 0
    line_range: list[int] = Field(default_factory=list)
    test_id: str = ""           # e.g. "B403"
    test_name: str = ""         # e.g. "blacklist"
    issue_severity: str = "LOW"  # LOW | MEDIUM | HIGH
    issue_confidence: str = "LOW"  # LOW | MEDIUM | HIGH
    issue_text: str = ""
    issue_cwe: dict | None = None
    code: str = ""
    more_info: str | None = None


class BanditReport(BaseModel):
    results: list[BanditResult] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    errors: list = Field(default_factory=list)


# --- Severity & category mapping --------------------------------------------


_SEVERITY_MAP = {
    "HIGH":   Severity.CRITICAL,
    "MEDIUM": Severity.ERROR,
    "LOW":    Severity.WARNING,
}


# bandit test_id → review category. Defaults to security since bandit is a
# security-focused linter, but a few of its checks are convention/style.
_CATEGORY_OVERRIDES = {
    "B101": ReviewCategory.CONVENTION,   # assert_used (style in production)
}


def _map_severity(diag: BanditResult) -> Severity:
    base = _SEVERITY_MAP.get(diag.issue_severity, Severity.WARNING)
    # If confidence is LOW, downgrade one notch
    if diag.issue_confidence == "LOW":
        if base == Severity.CRITICAL:
            return Severity.ERROR
        if base == Severity.ERROR:
            return Severity.WARNING
        return Severity.INFO
    return base


def _map_category(diag: BanditResult) -> ReviewCategory:
    override = _CATEGORY_OVERRIDES.get(diag.test_id)
    if override:
        return override
    return ReviewCategory.SECURITY


# --- Runner ------------------------------------------------------------------


class BanditRunner:
    """Run bandit on a path; return parsed results + Finding conversions."""

    DEFAULT_TIMEOUT_SECONDS = 120

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    # ---- Public API ----

    def scan(self, target: Path | str) -> BanditReport:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        # `-r` for directories; bandit handles single files too.
        cmd = [self.binary, "-r", "-f", "json", str(target)]
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
            raise BanditExecutionError(
                f"bandit timed out after {self.timeout}s on {target}"
            ) from e

        if not proc.stdout.strip():
            stderr = (proc.stderr or "").strip()
            raise BanditExecutionError(
                f"bandit produced no JSON (exit={proc.returncode}, stderr={stderr[:300]})"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise BanditExecutionError(
                f"bandit JSON parse failed: {e}; prefix={proc.stdout[:200]!r}"
            ) from e

        return BanditReport.model_validate(data)

    def scan_to_findings(
        self,
        target: Path | str,
        *,
        repo_root: Path | str | None = None,
    ) -> list[Finding]:
        report = self.scan(target)
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(r, root) for r in report.results]

    # ---- Internals ----

    @staticmethod
    def _locate_binary() -> str:
        env_path = os.environ.get("REVIO_BANDIT_BIN")
        if env_path and Path(env_path).is_file():
            return env_path

        # Prefer the bandit installed in the same Python environment as revio
        # (sys.executable's bin/ dir). This is the venv if running inside one.
        import sys
        sibling = Path(sys.executable).parent / "bandit"
        if sibling.is_file():
            return str(sibling)

        which = shutil.which("bandit")
        if which:
            return which

        raise BanditNotInstalledError(
            "bandit not found. Install with: pip install bandit "
            "(or set REVIO_BANDIT_BIN to its path)"
        )

    @staticmethod
    def _to_finding(diag: BanditResult, repo_root: Path | None) -> Finding:
        # Compute relative path
        file_path = diag.filename
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass

        evidence: list[Evidence] = [
            Evidence(
                kind="static_rule",
                summary=f"bandit {diag.test_id} ({diag.test_name}): {diag.issue_text}",
                source=f"bandit:{diag.test_id}",
            )
        ]
        if diag.code:
            evidence.append(Evidence(
                kind="code_excerpt",
                summary=diag.code.split("\n", 1)[0][:200],
                detail=diag.code,
                source=f"{file_path}:{diag.line_number}",
            ))
        if diag.issue_cwe and isinstance(diag.issue_cwe, dict) and diag.issue_cwe.get("id"):
            evidence.append(Evidence(
                kind="cross_reference",
                summary=f"CWE-{diag.issue_cwe['id']}: {diag.issue_cwe.get('link', '')}",
                source="cwe",
            ))

        title = diag.issue_text
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=diag.line_number,
            line_end=diag.line_range[-1] if len(diag.line_range) > 1 else None,
            severity=_map_severity(diag),
            category=_map_category(diag),
            title=title,
            hypothesis=f"bandit rule '{diag.test_id}' triggered: {diag.issue_text}",
            evidence=evidence,
            confidence=0.85 if diag.issue_confidence == "HIGH" else 0.65,
            verified=True,
            suggestion=diag.more_info,
            detected_by="static",
        )
