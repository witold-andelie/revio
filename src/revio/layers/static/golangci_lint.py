"""golangci-lint subprocess wrapper — composite Go linter.

golangci-lint bundles 100+ Go linters (govet, staticcheck, errcheck, gosec,
unused, ineffassign, ...) with parallel execution and JSON output. It's the
de-facto Go quality tool in the modern ecosystem.

Install:
    brew install golangci-lint                                 # macOS
    # OR
    curl -sSfL https://raw.githubusercontent.com/golangci/...  # script

Usage:
    cd <go module dir>
    golangci-lint run --output.json.path stdout

JSON output format (v2):
    {
      "Issues": [
        {"FromLinter": "gosec", "Text": "...", "Severity": "",
         "Pos": {"Filename": "main.go", "Line": 42, "Column": 5}},
        ...
      ],
      "Report": {...}
    }
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


class GolangCILintNotInstalledError(RuntimeError):
    """golangci-lint binary not found.

    Install via:  brew install golangci-lint    (macOS)
                  go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest
    """


class GolangCILintExecutionError(RuntimeError):
    """golangci-lint ran but produced unusable output."""


# --- Models ------------------------------------------------------------------


class GoLintPos(BaseModel):
    Filename: str = ""
    Line: int = 0
    Column: int = 0
    Offset: int = 0


class GoLintIssue(BaseModel):
    FromLinter: str = ""          # which sub-linter fired (gosec, govet, ...)
    Text: str = ""
    Severity: str = ""            # often empty in v2; default "warning"
    Pos: GoLintPos = Field(default_factory=GoLintPos)
    SourceLines: list[str] = Field(default_factory=list)


class GoLintReport(BaseModel):
    Issues: list[GoLintIssue] = Field(default_factory=list)


# --- Linter → category routing ----------------------------------------------


# Each sub-linter has a primary concern. Used to map findings to revio categories.
_LINTER_CATEGORY: dict[str, ReviewCategory] = {
    # Security
    "gosec":         ReviewCategory.SECURITY,
    "noctx":         ReviewCategory.SECURITY,  # network calls without context
    # Bugs / correctness
    "govet":         ReviewCategory.POTENTIAL_BUG,
    "staticcheck":   ReviewCategory.POTENTIAL_BUG,
    "errcheck":      ReviewCategory.POTENTIAL_BUG,
    "typecheck":     ReviewCategory.POTENTIAL_BUG,
    "ineffassign":   ReviewCategory.POTENTIAL_BUG,
    "errorlint":     ReviewCategory.POTENTIAL_BUG,
    "nilerr":        ReviewCategory.POTENTIAL_BUG,
    "rowserrcheck":  ReviewCategory.POTENTIAL_BUG,
    "bodyclose":     ReviewCategory.POTENTIAL_BUG,
    "sqlclosecheck": ReviewCategory.POTENTIAL_BUG,
    "contextcheck":  ReviewCategory.POTENTIAL_BUG,
    # Performance
    "prealloc":      ReviewCategory.PERFORMANCE,
    "perfsprint":    ReviewCategory.PERFORMANCE,
    # Redundancy / dead code
    "unused":        ReviewCategory.REDUNDANCY,
    "unparam":       ReviewCategory.REDUNDANCY,
    "deadcode":      ReviewCategory.REDUNDANCY,
    "varcheck":      ReviewCategory.REDUNDANCY,
    # Style / readability — default category
}


def _map_severity(issue: GoLintIssue) -> Severity:
    sev = (issue.Severity or "").lower()
    if sev == "error":
        return Severity.ERROR
    if sev == "warning":
        return Severity.WARNING
    # Linter-specific overrides for unmarked severity
    if issue.FromLinter == "gosec":
        return Severity.ERROR
    if issue.FromLinter in {"govet", "staticcheck", "typecheck", "errcheck"}:
        return Severity.WARNING
    return Severity.INFO


def _map_category(issue: GoLintIssue) -> ReviewCategory:
    return _LINTER_CATEGORY.get(issue.FromLinter, ReviewCategory.CODE_STYLE)


# --- Runner ------------------------------------------------------------------


class GolangCILintRunner:
    """Run golangci-lint on a Go module and return parsed issues + findings."""

    DEFAULT_TIMEOUT_SECONDS = 300

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    # ---- Public API ----

    def scan(self, target_dir: Path | str) -> list[GoLintIssue]:
        """Run golangci-lint on a Go module directory."""
        target = Path(target_dir).resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"target dir does not exist: {target}")

        # golangci-lint needs a go.mod somewhere up the tree
        if not self._has_go_mod(target):
            logger.info("golangci-lint: no go.mod near %s — skipping", target)
            return []

        cmd = [
            self.binary, "run",
            "--output.json.path", "stdout",
            "--issues-exit-code", "0",  # always exit 0 so we see JSON regardless
            "./...",
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
            raise GolangCILintExecutionError(
                f"golangci-lint timed out after {self.timeout}s"
            ) from e

        # golangci-lint may prepend non-JSON warnings; find the first '{' boundary
        text = proc.stdout
        brace = text.find("{")
        if brace < 0:
            stderr = (proc.stderr or "").strip()
            raise GolangCILintExecutionError(
                f"golangci-lint produced no JSON (exit={proc.returncode}, "
                f"stderr={stderr[:300]})"
            )

        # The JSON is one big object on a single line typically
        try:
            data = json.loads(text[brace:])
        except json.JSONDecodeError as e:
            # Try line-by-line — some configurations stream
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("{") and '"Issues"' in line:
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            else:
                raise GolangCILintExecutionError(
                    f"golangci-lint JSON parse failed: {e}"
                ) from e

        report = GoLintReport.model_validate(data)
        return report.Issues

    def scan_to_findings(
        self,
        target_dir: Path | str,
        *,
        repo_root: Path | str | None = None,
    ) -> list[Finding]:
        issues = self.scan(target_dir)
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(i, root, Path(target_dir).resolve()) for i in issues]

    # ---- Internals ----

    @staticmethod
    def _has_go_mod(dir_path: Path) -> bool:
        cur = dir_path
        for _ in range(5):  # walk up at most 5 levels
            if (cur / "go.mod").is_file():
                return True
            if cur.parent == cur:
                break
            cur = cur.parent
        return False

    @staticmethod
    def _locate_binary() -> str:
        env_path = os.environ.get("REVIO_GOLANGCI_LINT_BIN")
        if env_path and Path(env_path).is_file():
            return env_path

        for cand in ("/opt/homebrew/bin/golangci-lint", "/usr/local/bin/golangci-lint"):
            if Path(cand).is_file():
                return cand

        which = shutil.which("golangci-lint")
        if which:
            return which

        raise GolangCILintNotInstalledError(
            "golangci-lint not found. Install:\n"
            "  brew install golangci-lint   (macOS)\n"
            "  go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest"
        )

    @staticmethod
    def _to_finding(
        issue: GoLintIssue, repo_root: Path | None, scan_root: Path
    ) -> Finding:
        # Pos.Filename may be relative to scan_root
        file_path = issue.Pos.Filename
        abs_path = (scan_root / file_path).resolve() if not Path(file_path).is_absolute() else Path(file_path)
        if repo_root:
            try:
                file_path = str(abs_path.relative_to(repo_root))
            except ValueError:
                file_path = abs_path.name

        evidence: list[Evidence] = [
            Evidence(
                kind="static_rule",
                summary=f"golangci-lint/{issue.FromLinter}: {issue.Text}",
                source=f"golangci:{issue.FromLinter}",
            )
        ]
        if issue.SourceLines:
            evidence.append(Evidence(
                kind="code_excerpt",
                summary=issue.SourceLines[0][:200],
                detail="\n".join(issue.SourceLines),
                source=f"{file_path}:{issue.Pos.Line}",
            ))

        title = issue.Text
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=issue.Pos.Line or 1,
            severity=_map_severity(issue),
            category=_map_category(issue),
            title=title,
            hypothesis=f"golangci-lint/{issue.FromLinter}: {issue.Text}",
            evidence=evidence,
            confidence=0.9,
            verified=True,
            detected_by="static",
        )
