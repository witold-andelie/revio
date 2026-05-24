"""phpstan subprocess wrapper — PHP static analyzer.

phpstan finds bugs without running the code. Level 0 (loose) → 9 (strict).
Default level 5 (medium-strict, the maintainers' recommended starting point).

  phpstan analyse --no-progress --error-format json --level <N> <target>
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


class PhpstanNotInstalledError(RuntimeError):
    pass


class PhpstanExecutionError(RuntimeError):
    pass


class PhpstanMessage(BaseModel):
    message: str = ""
    line: int = 0
    ignorable: bool = True
    identifier: str | None = None


class PhpstanFile(BaseModel):
    errors: int = 0
    messages: list[PhpstanMessage] = Field(default_factory=list)


def _map_category(identifier: str | None, msg: str) -> ReviewCategory:
    s = (identifier or "").lower() + " " + msg.lower()
    if any(k in s for k in ("sql injection", "xss", "csrf", "unserialize", "eval")):
        return ReviewCategory.SECURITY
    if "deprecated" in s:
        return ReviewCategory.CONVENTION
    return ReviewCategory.POTENTIAL_BUG


class PhpstanRunner:
    DEFAULT_TIMEOUT_SECONDS = 300
    DEFAULT_LEVEL = 5

    def __init__(self, binary: str | None = None, timeout: int | None = None,
                 level: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS
        self.level = level if level is not None else \
            int(os.environ.get("REVIO_PHPSTAN_LEVEL", self.DEFAULT_LEVEL))

    def scan(self, target: Path | str) -> dict[str, PhpstanFile]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        cmd = [self.binary, "analyse", "--no-progress",
               "--error-format", "json", "--level", str(self.level),
               str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise PhpstanExecutionError(f"phpstan timed out on {target}") from e

        if not proc.stdout.strip():
            return {}
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise PhpstanExecutionError(
                f"phpstan JSON parse failed: {e}; prefix={proc.stdout[:200]!r}"
            ) from e
        files_dict = data.get("files", {}) or {}
        return {path: PhpstanFile.model_validate(info)
                for path, info in files_dict.items()}

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        out: list[Finding] = []
        for path, info in self.scan(target).items():
            for m in info.messages:
                out.append(self._to_finding(path, m, root))
        return out

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_PHPSTAN_BIN")
        if env and Path(env).is_file():
            return env
        # phpstan can be installed as phar or via composer; both end up as 'phpstan'
        which = shutil.which("phpstan")
        if which:
            return which
        raise PhpstanNotInstalledError(
            "phpstan not found. Install with: composer global require phpstan/phpstan "
            "or download phpstan.phar from https://phpstan.org/"
        )

    @staticmethod
    def _to_finding(filepath: str, m: PhpstanMessage,
                    repo_root: Path | None) -> Finding:
        file_path = filepath
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass
        title = m.message
        if len(title) > 80:
            title = title[:77] + "..."

        rule_id = m.identifier or "phpstan"
        return Finding(
            file_path=file_path,
            line_start=m.line,
            line_end=None,
            severity=Severity.WARNING,
            category=_map_category(m.identifier, m.message),
            title=title,
            hypothesis=f"phpstan flagged '{rule_id}': {m.message}",
            evidence=[Evidence(
                kind="static_rule",
                summary=f"phpstan ({rule_id}): {m.message}",
                source=f"phpstan:{rule_id}",
            )],
            confidence=0.85,
            verified=True,
            detected_by="static",
        )
