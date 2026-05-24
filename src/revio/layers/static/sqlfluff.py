"""sqlfluff subprocess wrapper — SQL linter with dialect support.

sqlfluff handles 20+ dialects (postgres / mysql / snowflake / bigquery /
redshift / sqlite / ...). Auto-detects via `--dialect ansi` default;
override via SQLFLUFF_DIALECT env or per-call.

  sqlfluff lint --format json --nofail [--dialect <D>] <target>

sqlfluff is a pip package (Python), so it's always installable in revio's
own venv via `pip install sqlfluff` — no system package manager needed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from ...output.models import Evidence, Finding, ReviewCategory, Severity


logger = logging.getLogger(__name__)


class SqlfluffNotInstalledError(RuntimeError):
    pass


class SqlfluffExecutionError(RuntimeError):
    pass


class SqlfluffViolation(BaseModel):
    line_no: int = 0
    line_pos: int = 0
    code: str = ""            # e.g. "L010", "PRS"
    description: str = ""
    name: str = ""            # e.g. "capitalisation.keywords"


class SqlfluffFileReport(BaseModel):
    filepath: str = ""
    violations: list[SqlfluffViolation] = Field(default_factory=list)


# sqlfluff's rule codes: P/L/A/C/J/T/S etc. — prefix correlates with category
_PARSE_ERROR_CODES = {"PRS", "LXR", "TMP"}  # syntax / lexing / templating
_STYLE_PREFIXES = ("L00", "L01", "L02", "L03")  # spacing / capitalisation


def _map_severity(code: str) -> Severity:
    if code in _PARSE_ERROR_CODES:
        return Severity.ERROR
    if code.startswith("L"):
        return Severity.WARNING
    return Severity.INFO


def _map_category(code: str) -> ReviewCategory:
    if code in _PARSE_ERROR_CODES:
        return ReviewCategory.POTENTIAL_BUG
    if any(code.startswith(p) for p in _STYLE_PREFIXES):
        return ReviewCategory.CODE_STYLE
    return ReviewCategory.CONVENTION


class SqlfluffRunner:
    DEFAULT_TIMEOUT_SECONDS = 120

    def __init__(self, binary: str | None = None, timeout: int | None = None,
                 dialect: str | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS
        self.dialect = dialect or os.environ.get("SQLFLUFF_DIALECT", "ansi")

    def scan(self, target: Path | str) -> list[SqlfluffFileReport]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        cmd = [self.binary, "lint", "--format", "json", "--nofail",
               "--dialect", self.dialect, str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise SqlfluffExecutionError(f"sqlfluff timed out on {target}") from e

        if not proc.stdout.strip():
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise SqlfluffExecutionError(
                f"sqlfluff JSON parse failed: {e}; prefix={proc.stdout[:200]!r}"
            ) from e
        # sqlfluff returns a list of file reports
        return [SqlfluffFileReport.model_validate(r) for r in data]

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        out: list[Finding] = []
        for report in self.scan(target):
            for v in report.violations:
                out.append(self._to_finding(report.filepath, v, root))
        return out

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_SQLFLUFF_BIN")
        if env and Path(env).is_file():
            return env
        # Prefer revio venv-local sqlfluff
        sibling = Path(sys.executable).parent / "sqlfluff"
        if sibling.is_file():
            return str(sibling)
        which = shutil.which("sqlfluff")
        if which:
            return which
        raise SqlfluffNotInstalledError(
            "sqlfluff not found. Install with: pip install sqlfluff"
        )

    @staticmethod
    def _to_finding(filepath: str, v: SqlfluffViolation,
                    repo_root: Path | None) -> Finding:
        file_path = filepath
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass
        title = v.description
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=v.line_no,
            line_end=None,
            severity=_map_severity(v.code),
            category=_map_category(v.code),
            title=title,
            hypothesis=f"sqlfluff rule '{v.code}' triggered: {v.description}",
            evidence=[Evidence(
                kind="static_rule",
                summary=f"sqlfluff {v.code} ({v.name or 'unknown'}): {v.description}",
                source=f"sqlfluff:{v.code}",
            )],
            confidence=0.95 if v.code in _PARSE_ERROR_CODES else 0.8,
            verified=True,
            detected_by="static",
        )
