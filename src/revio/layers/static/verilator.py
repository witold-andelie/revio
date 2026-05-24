"""verilator subprocess wrapper — Verilog / SystemVerilog linter.

verilator is the de-facto open-source SystemVerilog tool. We use its
`--lint-only` mode which runs the front-end + lint passes without actually
elaborating to C++. Output goes to stderr in a fairly stable text format
that we parse with a regex.

  verilator --lint-only -Wall <files>

Output line shape:
  %Warning-WIDTH: file.v:LINE:COL: <message>
  %Error: file.v:LINE:COL: <message>
  %Error-SYNTAX: file.v:LINE:COL: syntax error, unexpected ...

We extract the severity (Warning vs Error vs Info), the rule tag
(WIDTH / UNUSED / CASEINCOMPLETE / etc.), file/line/col, and the message.

Single binary; brew / apt / scoop installable cross-platform.
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


class VerilatorNotInstalledError(RuntimeError):
    pass


class VerilatorExecutionError(RuntimeError):
    pass


class VerilatorDiagnostic(BaseModel):
    file: str = ""
    line: int = 0
    col: int = 0
    severity: str = "warning"   # error | warning | info
    rule: str = ""              # e.g. "WIDTH", "UNUSED", "" if none
    message: str = ""


# Examples we need to parse:
#   %Warning-WIDTH: foo.v:12:3: Operator ASSIGN expects 8 bits ...
#   %Warning-UNUSED: foo.v:5:7: Signal is not used: 'unused_wire'
#   %Error: foo.v:42:1: syntax error, unexpected 'endmodule'
#   %Error-SYNTAX: foo.v:42:1: ...
_LINE_RE = re.compile(
    r"^%(?P<sev>Error|Warning|Info)"
    r"(?:-(?P<rule>[A-Z_]+))?"
    r":\s*"
    r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<msg>.+)$"
)


_SEVERITY_MAP = {
    "error":   Severity.ERROR,
    "warning": Severity.WARNING,
    "info":    Severity.INFO,
}


# verilator rule → revio category. Synthesis-correctness issues are
# treated as potential bugs (they survive into silicon); style stuff stays style.
_BUG_RULES = {
    "WIDTH", "WIDTHCONCAT", "WIDTHTRUNC", "WIDTHEXPAND",   # bit-width mismatches
    "CASEINCOMPLETE", "CASEX", "CASEOVERLAP",              # latch-inferring case
    "BLKSEQ", "BLKANDNBLK",                                # blocking/non-blocking mix
    "LATCH",                                               # combinational latch
    "MULTIDRIVEN", "MULTIDRIVE",                           # multi-driven nets
    "ASYNCBR", "ASYNC",                                    # async logic / CDC
    "UNDRIVEN", "PINMISSING", "PINNOTFOUND",               # connectivity errors
    "STMTDLY", "INFINITELOOP",                             # simulation hazards
    "REALCVT",                                             # real → integer conversions
}

_STYLE_RULES = {
    "UNUSED", "UNUSEDPARAM", "UNUSEDSIGNAL",   # unused declarations
    "DECLFILENAME",                              # filename ≠ module name
    "EOFNEWLINE",                                # whitespace
    "VARHIDDEN",                                 # shadowing
}


def _map_category(rule: str, severity: str) -> ReviewCategory:
    if not rule:
        # No rule tag → most likely a syntax / parse error
        return ReviewCategory.POTENTIAL_BUG if severity == "error" else ReviewCategory.CONVENTION
    if rule in _BUG_RULES:
        return ReviewCategory.POTENTIAL_BUG
    if rule in _STYLE_RULES:
        return ReviewCategory.CODE_STYLE
    return ReviewCategory.CONVENTION


class VerilatorRunner:
    DEFAULT_TIMEOUT_SECONDS = 180

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    def scan(self, target: Path | str) -> list[VerilatorDiagnostic]:
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        # verilator wants individual files; expand directory to .v/.sv
        files: list[Path]
        if target.is_dir():
            files = [p for p in target.rglob("*")
                     if p.is_file() and p.suffix in {".v", ".vh", ".sv", ".svh"}]
            if not files:
                return []
        else:
            files = [target]

        # --lint-only does the parse + lint passes but skips elaboration to C++
        # -Wall enables all warnings; we let the wrapper filter noise via category mapping
        cmd = [self.binary, "--lint-only", "-Wall", *map(str, files)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise VerilatorExecutionError(f"verilator timed out on {target}") from e

        # verilator emits its diagnostics on STDERR (not stdout)
        out: list[VerilatorDiagnostic] = []
        for raw in (proc.stderr or "").splitlines():
            m = _LINE_RE.match(raw.strip())
            if not m:
                continue
            out.append(VerilatorDiagnostic(
                file=m.group("file"),
                line=int(m.group("line")),
                col=int(m.group("col")),
                severity=m.group("sev").lower(),
                rule=m.group("rule") or "",
                message=m.group("msg").strip(),
            ))
        return out

    def scan_to_findings(self, target: Path | str, *,
                         repo_root: Path | str | None = None) -> list[Finding]:
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(d, root) for d in self.scan(target)]

    @staticmethod
    def _locate_binary() -> str:
        env = os.environ.get("REVIO_VERILATOR_BIN")
        if env and Path(env).is_file():
            return env
        which = shutil.which("verilator")
        if which:
            return which
        raise VerilatorNotInstalledError(
            "verilator not found. Install with: brew install verilator (macOS), "
            "apt install verilator (Linux), or scoop install verilator (Windows)"
        )

    @staticmethod
    def _to_finding(d: VerilatorDiagnostic, repo_root: Path | None) -> Finding:
        file_path = d.file
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass
        rule_tag = d.rule or "SYNTAX"
        title = d.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=d.line,
            line_end=None,
            severity=_SEVERITY_MAP.get(d.severity, Severity.WARNING),
            category=_map_category(d.rule, d.severity),
            title=title,
            hypothesis=f"verilator '{rule_tag}' triggered: {d.message}",
            evidence=[
                Evidence(
                    kind="static_rule",
                    summary=f"verilator {rule_tag} ({d.severity}): {d.message}",
                    source=f"verilator:{rule_tag}",
                ),
                Evidence(
                    kind="cross_reference",
                    summary=(
                        f"https://verilator.org/warn/{rule_tag}.html"
                        if d.rule else "https://verilator.org/guide/latest/warnings.html"
                    ),
                    source="verilator-docs",
                ),
            ],
            confidence=0.9,
            verified=True,
            detected_by="static",
        )
