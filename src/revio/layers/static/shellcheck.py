"""shellcheck subprocess wrapper — Bash/sh/zsh static analyzer.

shellcheck is the de-facto Shell linter. Catches quoting bugs, glob mistakes,
subshell variable scope errors, exit-code masking, $IFS pitfalls, etc.

  shellcheck -f json <target>   → JSON array of diagnostics

Single binary, cross-platform (brew / apt / winget / scoop).
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


class ShellcheckNotInstalledError(RuntimeError):
    """shellcheck binary not found."""


class ShellcheckExecutionError(RuntimeError):
    """shellcheck ran but produced unusable output."""


class ShellcheckDiagnostic(BaseModel):
    file: str = ""
    line: int = 0
    endLine: int | None = None
    column: int = 0
    endColumn: int | None = None
    level: str = "warning"   # error | warning | info | style
    code: int = 0
    message: str = ""


_SEVERITY_MAP = {
    "error":   Severity.ERROR,
    "warning": Severity.WARNING,
    "info":    Severity.INFO,
    "style":   Severity.INFO,
}

# A handful of shellcheck codes that are actually security-flavored
_SECURITY_CODES = {2086, 2046, 2153, 2154, 2155}  # word-splitting / unquoted exec inputs


def _map_category(code: int, level: str) -> ReviewCategory:
    if code in _SECURITY_CODES:
        return ReviewCategory.SECURITY
    if level == "error":
        return ReviewCategory.POTENTIAL_BUG
    if level == "style":
        return ReviewCategory.CODE_STYLE
    return ReviewCategory.POTENTIAL_BUG


class ShellcheckRunner:
    DEFAULT_TIMEOUT_SECONDS = 90

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    def scan(self, target: Path | str) -> list[ShellcheckDiagnostic]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        # shellcheck wants individual files; if a dir, expand .sh/.bash
        files: list[Path]
        if target.is_dir():
            files = [p for p in target.rglob("*")
                     if p.is_file() and p.suffix in {".sh", ".bash", ".zsh"}]
            if not files:
                return []
        else:
            files = [target]

        cmd = [self.binary, "-f", "json", *map(str, files)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise ShellcheckExecutionError(f"shellcheck timed out on {target}") from e

        if not proc.stdout.strip():
            return []  # no findings == no JSON; exit code is non-zero only when bugs found
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ShellcheckExecutionError(f"shellcheck JSON parse failed: {e}") from e
        return [ShellcheckDiagnostic.model_validate(d) for d in data]

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(d, root) for d in self.scan(target)]

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_SHELLCHECK_BIN")
        if env and Path(env).is_file():
            return env
        which = shutil.which("shellcheck")
        if which:
            return which
        raise ShellcheckNotInstalledError(
            "shellcheck not found. Install with: brew install shellcheck "
            "(macOS) / apt install shellcheck (Linux) / "
            "winget install ShellCheck (Windows)"
        )

    @staticmethod
    def _to_finding(d: ShellcheckDiagnostic, repo_root: Path | None) -> Finding:
        file_path = d.file
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass

        code = f"SC{d.code}"
        evidence = [
            Evidence(
                kind="static_rule",
                summary=f"shellcheck {code} ({d.level}): {d.message}",
                source=f"shellcheck:{code}",
            ),
            Evidence(
                kind="cross_reference",
                summary=f"https://www.shellcheck.net/wiki/{code}",
                source="shellcheck-wiki",
            ),
        ]
        title = d.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=d.line,
            line_end=d.endLine,
            severity=_SEVERITY_MAP.get(d.level, Severity.WARNING),
            category=_map_category(d.code, d.level),
            title=title,
            hypothesis=f"shellcheck rule '{code}' triggered: {d.message}",
            evidence=evidence,
            confidence=0.9,
            verified=True,
            detected_by="static",
        )
