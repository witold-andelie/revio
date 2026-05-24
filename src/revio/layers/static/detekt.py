"""detekt subprocess wrapper — Kotlin static analyzer.

detekt is the Kotlin standard linter. Style + complexity + potential bugs +
naming conventions; lots of rules; runs as a standalone CLI jar.

  detekt -i <target> --report json:<tmp.json>

Unlike most analyzers, detekt writes its JSON report to a FILE (not stdout),
so we tell it to write to a temp file and read it back. Detekt also exits
non-zero on findings — we ignore the exit code.

Requires a JDK (same constraint as the spotbugs wrapper).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from ...output.models import Evidence, Finding, ReviewCategory, Severity


logger = logging.getLogger(__name__)


class DetektNotInstalledError(RuntimeError):
    pass


class DetektExecutionError(RuntimeError):
    pass


class DetektLocation(BaseModel):
    startLine: int = 0
    startColumn: int = 0
    endLine: int | None = None
    endColumn: int | None = None


class DetektIssue(BaseModel):
    ruleId: str = ""
    severity: str = "warning"   # info / warning / error
    message: str = ""
    location: DetektLocation = Field(default_factory=DetektLocation)


class DetektFile(BaseModel):
    path: str = ""
    issues: list[DetektIssue] = Field(default_factory=list)


_SEVERITY_MAP = {
    "fatal":   Severity.CRITICAL,
    "error":   Severity.ERROR,
    "warning": Severity.WARNING,
    "info":    Severity.INFO,
}


def _map_category(rule_id: str) -> ReviewCategory:
    s = rule_id.lower()
    if "complexity" in s:
        return ReviewCategory.ARCHITECTURE
    if "performance" in s:
        return ReviewCategory.PERFORMANCE
    if "potential-bugs" in s or "exception" in s:
        return ReviewCategory.POTENTIAL_BUG
    if "style" in s or "formatting" in s or "naming" in s:
        return ReviewCategory.CODE_STYLE
    return ReviewCategory.CONVENTION


class DetektRunner:
    DEFAULT_TIMEOUT_SECONDS = 300

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    def scan(self, target: Path | str) -> list[DetektFile]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        # detekt writes its report to a file — give it a temp path
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            report_path = tmp.name
        try:
            cmd = [self.binary, "-i", str(target),
                   "--report", f"json:{report_path}"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as e:
                raise DetektExecutionError(f"detekt timed out on {target}") from e

            if not Path(report_path).is_file():
                # detekt printed something to stderr — bubble it up
                stderr = (proc.stderr or "").strip()
                raise DetektExecutionError(
                    f"detekt produced no report (exit={proc.returncode}, "
                    f"stderr={stderr[:200]})"
                )
            try:
                with open(report_path, encoding="utf-8") as fh:
                    raw = fh.read().strip()
                if not raw:
                    return []
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise DetektExecutionError(f"detekt JSON parse failed: {e}") from e
        finally:
            try:
                os.unlink(report_path)
            except OSError:
                pass

        files = []
        # detekt JSON shape: {files: [{path, issues: [...]}]} on recent versions
        # but some versions emit a flat list. Handle both.
        if isinstance(data, dict) and "files" in data:
            for f in data["files"]:
                files.append(DetektFile.model_validate(f))
        elif isinstance(data, list):
            # SARIF-ish flat — group by path
            by_path: dict[str, list[dict]] = {}
            for issue in data:
                path = issue.get("location", {}).get("file", "")
                by_path.setdefault(path, []).append(issue)
            for path, issues in by_path.items():
                files.append(DetektFile(path=path, issues=[
                    DetektIssue.model_validate(i) for i in issues
                ]))
        return files

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        out: list[Finding] = []
        for f in self.scan(target):
            for i in f.issues:
                out.append(self._to_finding(f.path, i, root))
        return out

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_DETEKT_BIN")
        if env and Path(env).is_file():
            return env
        for name in ("detekt", "detekt-cli"):
            which = shutil.which(name)
            if which:
                return which
        raise DetektNotInstalledError(
            "detekt not found. Install with: brew install detekt (macOS), "
            "or download detekt-cli from https://github.com/detekt/detekt/releases"
        )

    @staticmethod
    def _to_finding(filepath: str, i: DetektIssue,
                    repo_root: Path | None) -> Finding:
        file_path = filepath
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass
        title = i.message
        if len(title) > 80:
            title = title[:77] + "..."
        return Finding(
            file_path=file_path,
            line_start=i.location.startLine,
            line_end=i.location.endLine,
            severity=_SEVERITY_MAP.get(i.severity, Severity.WARNING),
            category=_map_category(i.ruleId),
            title=title,
            hypothesis=f"detekt rule '{i.ruleId}' triggered: {i.message}",
            evidence=[Evidence(
                kind="static_rule",
                summary=f"detekt {i.ruleId} ({i.severity}): {i.message}",
                source=f"detekt:{i.ruleId}",
            )],
            confidence=0.85,
            verified=True,
            detected_by="static",
        )
