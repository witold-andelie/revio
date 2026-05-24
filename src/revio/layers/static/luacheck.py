"""luacheck subprocess wrapper — Lua static analyzer.

luacheck catches unused variables, shadowing, global pollution, suspicious
patterns. Single binary, mature, fast.

  luacheck --no-color --codes --ranges --formatter plain <target>

The `plain` formatter emits lines like:
  path/to/file.lua:LINE:COL-ENDCOL: (W211) unused variable 'foo'
  path/to/file.lua:LINE:COL-ENDCOL: (E001) unexpected symbol near '<eof>'

Warning codes Wxxx (1xx-9xx) and error codes Exxx are documented at
https://luacheck.readthedocs.io/en/stable/warnings.html.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from ...output.models import Evidence, Finding, ReviewCategory, Severity


logger = logging.getLogger(__name__)


class LuacheckNotInstalledError(RuntimeError):
    pass


class LuacheckExecutionError(RuntimeError):
    pass


class LuacheckDiagnostic(BaseModel):
    file: str = ""
    line: int = 0
    col: int = 0
    end_col: int | None = None
    code: str = ""           # "W211", "E001", etc.
    message: str = ""

    @property
    def is_error(self) -> bool:
        return self.code.startswith("E")


# Pattern: file:line:col[-end_col]: (CODE) message
_LINE_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)(?:-(?P<end_col>\d+))?:\s*"
    r"\((?P<code>[EW]\d+)\)\s+(?P<msg>.+)$"
)


def _map_severity(code: str) -> Severity:
    if code.startswith("E"):
        return Severity.ERROR
    # W1xx = unused, W2xx = redefined/shadow, W3xx = control flow,
    # W4xx = assignment, W5xx = standard lib, W6xx = whitespace
    if code.startswith(("W3", "W5")):  # control flow + bad stdlib calls
        return Severity.WARNING
    return Severity.INFO


_STYLE_PREFIXES = ("W6",)  # whitespace
_BUG_PREFIXES = ("E", "W3", "W4", "W5")


def _map_category(code: str) -> ReviewCategory:
    if code.startswith(_STYLE_PREFIXES):
        return ReviewCategory.CODE_STYLE
    if code.startswith(_BUG_PREFIXES):
        return ReviewCategory.POTENTIAL_BUG
    return ReviewCategory.CONVENTION


class LuacheckRunner:
    DEFAULT_TIMEOUT_SECONDS = 90

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    def scan(self, target: Path | str) -> list[LuacheckDiagnostic]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        cmd = [self.binary, "--no-color", "--codes", "--ranges",
               "--formatter", "plain", str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise LuacheckExecutionError(f"luacheck timed out on {target}") from e

        # luacheck exits 1 when any warnings, 2 when errors — output is on stdout
        out: list[LuacheckDiagnostic] = []
        for line in (proc.stdout or "").splitlines():
            m = _LINE_RE.match(line.strip())
            if not m:
                continue
            out.append(LuacheckDiagnostic(
                file=m.group("file"),
                line=int(m.group("line")),
                col=int(m.group("col")),
                end_col=int(m.group("end_col")) if m.group("end_col") else None,
                code=m.group("code"),
                message=m.group("msg"),
            ))
        return out

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(d, root) for d in self.scan(target)]

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_LUACHECK_BIN")
        if env and Path(env).is_file():
            return env
        which = shutil.which("luacheck")
        if which:
            return which
        raise LuacheckNotInstalledError(
            "luacheck not found. Install with: luarocks install luacheck "
            "(or: brew install luacheck on macOS, apt install lua-check on Linux)"
        )

    @staticmethod
    def _to_finding(d: LuacheckDiagnostic, repo_root: Path | None) -> Finding:
        file_path = d.file
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass
        title = d.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=d.line,
            line_end=None,
            severity=_map_severity(d.code),
            category=_map_category(d.code),
            title=title,
            hypothesis=f"luacheck rule '{d.code}' triggered: {d.message}",
            evidence=[Evidence(
                kind="static_rule",
                summary=f"luacheck {d.code}: {d.message}",
                source=f"luacheck:{d.code}",
            )],
            confidence=0.9 if d.is_error else 0.8,
            verified=True,
            detected_by="static",
        )
