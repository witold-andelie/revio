"""rubocop subprocess wrapper — Ruby static analyzer.

rubocop is the Ruby community's standard linter. Covers style, convention,
performance, and security (via the `rubocop-thread-safety` and built-in
security cops). For Rails-specific security holes (mass assignment, SQLi
template patterns, XSS) the user can additionally install `brakeman` —
not wrapped here yet but trivially addable.

  rubocop --format json [files...]
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


class RubocopNotInstalledError(RuntimeError):
    pass


class RubocopExecutionError(RuntimeError):
    pass


class RubocopLocation(BaseModel):
    line: int = 0
    column: int = 0
    last_line: int | None = None
    last_column: int | None = None


class RubocopOffense(BaseModel):
    severity: str = "warning"   # convention / refactor / warning / error / fatal
    message: str = ""
    cop_name: str = ""
    corrected: bool = False
    correctable: bool = False
    location: RubocopLocation = Field(default_factory=RubocopLocation)


class RubocopFile(BaseModel):
    path: str = ""
    offenses: list[RubocopOffense] = Field(default_factory=list)


_SEVERITY_MAP = {
    "fatal":       Severity.CRITICAL,
    "error":       Severity.ERROR,
    "warning":     Severity.WARNING,
    "refactor":    Severity.INFO,
    "convention":  Severity.INFO,
}


def _map_category(cop_name: str, severity: str) -> ReviewCategory:
    name = cop_name.lower()
    if "security" in name:
        return ReviewCategory.SECURITY
    if name.startswith(("performance/", "perf")):
        return ReviewCategory.PERFORMANCE
    if name.startswith("style/") or severity == "convention":
        return ReviewCategory.CODE_STYLE
    if severity in {"error", "fatal", "warning"}:
        return ReviewCategory.POTENTIAL_BUG
    return ReviewCategory.CONVENTION


class RubocopRunner:
    DEFAULT_TIMEOUT_SECONDS = 180

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    def scan(self, target: Path | str) -> list[RubocopFile]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        cmd = [self.binary, "--format", "json", str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise RubocopExecutionError(f"rubocop timed out on {target}") from e

        if not proc.stdout.strip():
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RubocopExecutionError(
                f"rubocop JSON parse failed: {e}; prefix={proc.stdout[:200]!r}"
            ) from e
        files = data.get("files", []) or []
        return [RubocopFile.model_validate(f) for f in files]

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        out: list[Finding] = []
        for f in self.scan(target):
            for o in f.offenses:
                out.append(self._to_finding(f.path, o, root))
        return out

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_RUBOCOP_BIN")
        if env and Path(env).is_file():
            return env
        which = shutil.which("rubocop")
        if which:
            return which
        raise RubocopNotInstalledError(
            "rubocop not found. Install with: gem install rubocop"
        )

    @staticmethod
    def _to_finding(filepath: str, o: RubocopOffense,
                    repo_root: Path | None) -> Finding:
        file_path = filepath
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass
        title = o.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=o.location.line,
            line_end=o.location.last_line,
            severity=_SEVERITY_MAP.get(o.severity, Severity.WARNING),
            category=_map_category(o.cop_name, o.severity),
            title=title,
            hypothesis=f"rubocop cop '{o.cop_name}' triggered: {o.message}",
            evidence=[Evidence(
                kind="static_rule",
                summary=f"rubocop {o.cop_name} ({o.severity}): {o.message}",
                source=f"rubocop:{o.cop_name}",
            )],
            confidence=0.9 if o.severity in {"error", "fatal"} else 0.75,
            verified=True,
            suggestion="(rubocop can auto-fix this)" if o.correctable else None,
            detected_by="static",
        )
