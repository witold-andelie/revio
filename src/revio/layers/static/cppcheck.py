"""cppcheck subprocess wrapper — C/C++ static analyzer.

cppcheck is the most-installed C/C++ analyzer (BSD-licensed, available on
every major Linux distro and via brew). Catches buffer overflows, null
dereferences, uninitialized variables, memory leaks, integer overflows,
and many others. Outputs XML via `--xml --xml-version=2`.

Install:
    brew install cppcheck                # macOS
    apt install cppcheck                  # Debian/Ubuntu
    yum install cppcheck                  # RHEL

XML format:
    <results version="2">
      <errors>
        <error id="..." severity="..." msg="..." cwe="..." file0="...">
          <location file="..." line="..." column="..." info="..."/>
          <location ...> (additional locations for context)
        </error>
        ...
      </errors>
    </results>
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, Field

from ...output.models import Evidence, Finding, ReviewCategory, Severity


logger = logging.getLogger(__name__)


# --- Errors ------------------------------------------------------------------


class CppcheckNotInstalledError(RuntimeError):
    """cppcheck binary not found.

    Install:  brew install cppcheck     (macOS)
              apt install cppcheck      (Debian/Ubuntu)
    """


class CppcheckExecutionError(RuntimeError):
    """cppcheck ran but produced unusable output."""


# --- Models ------------------------------------------------------------------


class CppCheckLocation(BaseModel):
    file: str = ""
    line: int = 0
    column: int = 0
    info: str | None = None


class CppCheckError(BaseModel):
    id: str = ""                  # e.g. "bufferAccessOutOfBounds"
    severity: str = "style"       # error | warning | style | performance | portability | information
    message: str = ""
    verbose: str = ""
    cwe: int | None = None
    locations: list[CppCheckLocation] = Field(default_factory=list)

    @property
    def primary_location(self) -> CppCheckLocation | None:
        return self.locations[0] if self.locations else None


# --- Severity / category mapping --------------------------------------------


_SEVERITY_MAP = {
    "error":         Severity.ERROR,
    "warning":       Severity.WARNING,
    "performance":   Severity.INFO,
    "portability":   Severity.INFO,
    "style":         Severity.INFO,
    "information":   Severity.INFO,
}


# Per-rule severity overrides — these are the high-impact correctness rules
# that should be elevated to CRITICAL.
_CRITICAL_RULES = frozenset({
    "bufferAccessOutOfBounds",
    "bufferOverflow",
    "stringBufferOverflow",
    "nullPointer",
    "nullPointerDefaultArg",
    "useAfterFree",
    "doubleFree",
    "memleak",                # may not warrant CRITICAL but enterprises care
    "uninitvar",              # uninitialized variable read
    "integerOverflow",
    "negativeIndex",
    "shiftTooManyBits",
    "wrongPrintfScanfArgNum",
    "writeReadOnlyFile",
    "deallocuse",
})


def _map_severity(err: CppCheckError) -> Severity:
    if err.id in _CRITICAL_RULES:
        return Severity.CRITICAL
    return _SEVERITY_MAP.get(err.severity, Severity.WARNING)


def _map_category(err: CppCheckError) -> ReviewCategory:
    sev = err.severity.lower()
    if sev == "performance":
        return ReviewCategory.PERFORMANCE
    if sev in {"style", "portability"}:
        return ReviewCategory.CODE_STYLE
    # error / warning — could be security or bug
    if err.cwe and err.cwe in {
        119, 120, 121, 122, 125, 126, 127,  # buffer-overflow CWEs
        416,  # use-after-free
        476,  # null deref
        190,  # integer overflow
        788,  # out-of-bounds access
        787,  # out-of-bounds write
    }:
        return ReviewCategory.SECURITY
    if sev in {"error", "warning"}:
        return ReviewCategory.POTENTIAL_BUG
    return ReviewCategory.CODE_STYLE


# --- Runner ------------------------------------------------------------------


class CppcheckRunner:
    """Run cppcheck on a C/C++ file or directory; return parsed errors."""

    DEFAULT_TIMEOUT_SECONDS = 300

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    # ---- Public API ----

    def scan(self, target: Path | str) -> list[CppCheckError]:
        """Run cppcheck with all enabled checks; return parsed errors."""
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        cmd = [
            self.binary,
            "--enable=warning,style,performance,portability",
            "--inline-suppr",
            "--xml", "--xml-version=2",
            "--quiet",
            str(target),
        ]
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
            raise CppcheckExecutionError(
                f"cppcheck timed out after {self.timeout}s on {target}"
            ) from e

        # cppcheck writes the XML to STDERR (not stdout), with progress info
        # interleaved. We want the XML block.
        xml_text = proc.stderr  # cppcheck quirk: XML goes to stderr
        if not xml_text.strip().startswith("<?xml"):
            # Maybe it's actually on stdout
            if proc.stdout.strip().startswith("<?xml"):
                xml_text = proc.stdout
            else:
                raise CppcheckExecutionError(
                    f"cppcheck produced no XML (exit={proc.returncode}, "
                    f"stdout-prefix={proc.stdout[:200]!r})"
                )

        return self._parse_xml(xml_text)

    def scan_to_findings(
        self,
        target: Path | str,
        *,
        repo_root: Path | str | None = None,
    ) -> list[Finding]:
        errors = self.scan(target)
        root = Path(repo_root).resolve() if repo_root else None
        return [
            self._to_finding(e, root)
            for e in errors
            if e.primary_location and e.id != "missingIncludeSystem"
        ]

    # ---- Internals ----

    @staticmethod
    def _locate_binary() -> str:
        env_path = os.environ.get("REVIO_CPPCHECK_BIN")
        if env_path and Path(env_path).is_file():
            return env_path

        for cand in ("/opt/homebrew/bin/cppcheck", "/usr/local/bin/cppcheck"):
            if Path(cand).is_file():
                return cand

        which = shutil.which("cppcheck")
        if which:
            return which

        raise CppcheckNotInstalledError(
            "cppcheck not found. Install:\n"
            "  brew install cppcheck   (macOS)\n"
            "  apt install cppcheck    (Debian/Ubuntu)"
        )

    @staticmethod
    def _parse_xml(xml_text: str) -> list[CppCheckError]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise CppcheckExecutionError(f"cppcheck XML parse failed: {e}") from e

        errors: list[CppCheckError] = []
        for err_el in root.findall(".//error"):
            try:
                cwe_attr = err_el.get("cwe")
                cwe = int(cwe_attr) if cwe_attr else None
            except ValueError:
                cwe = None

            locations: list[CppCheckLocation] = []
            for loc_el in err_el.findall("location"):
                try:
                    line = int(loc_el.get("line") or 0)
                    col = int(loc_el.get("column") or 0)
                except ValueError:
                    line = col = 0
                locations.append(CppCheckLocation(
                    file=loc_el.get("file") or "",
                    line=line,
                    column=col,
                    info=loc_el.get("info") or None,
                ))

            errors.append(CppCheckError(
                id=err_el.get("id") or "",
                severity=err_el.get("severity") or "style",
                message=err_el.get("msg") or "",
                verbose=err_el.get("verbose") or "",
                cwe=cwe,
                locations=locations,
            ))
        return errors

    @staticmethod
    def _to_finding(err: CppCheckError, repo_root: Path | None) -> Finding:
        loc = err.primary_location
        assert loc is not None  # caller filters

        file_path = loc.file
        if repo_root and file_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(repo_root))
            except ValueError:
                pass

        evidence: list[Evidence] = [
            Evidence(
                kind="static_rule",
                summary=f"cppcheck {err.id} ({err.severity}): {err.message}",
                source=f"cppcheck:{err.id}",
            )
        ]
        if err.verbose and err.verbose != err.message:
            evidence.append(Evidence(
                kind="reasoning",
                summary=err.verbose[:200],
                detail=err.verbose,
                source="cppcheck:verbose",
            ))
        # Add secondary locations as context (e.g. the assignment before a null deref)
        for sec_loc in err.locations[1:]:
            if sec_loc.info:
                evidence.append(Evidence(
                    kind="cross_reference",
                    summary=f"line {sec_loc.line}: {sec_loc.info}",
                    source=f"{sec_loc.file}:{sec_loc.line}",
                ))
        if err.cwe:
            evidence.append(Evidence(
                kind="cross_reference",
                summary=f"CWE-{err.cwe}",
                source="cwe",
            ))

        title = err.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=loc.line or 1,
            severity=_map_severity(err),
            category=_map_category(err),
            title=title,
            hypothesis=f"cppcheck '{err.id}' triggered: {err.message}",
            evidence=evidence,
            confidence=0.9 if err.severity in {"error", "warning"} else 0.7,
            verified=True,
            suggestion=err.verbose if err.verbose != err.message else None,
            detected_by="static",
        )
