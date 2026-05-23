"""SpotBugs subprocess wrapper — Java bug/security analyzer.

SpotBugs is the successor to FindBugs, the de-facto Java static analyzer.
With the FindSecBugs plugin it also covers OWASP-style security patterns
(SQL injection, XSS, weak crypto, XXE, etc.).

Install:
    brew install spotbugs               # macOS
    # OR download from https://spotbugs.github.io/

Usage:
    spotbugs -textui -xml:withMessages -output report.xml <target>

The wrapper:
- Locates the spotbugs binary (env var → PATH → common install locations)
- Runs spotbugs in textui mode with XML output
- Parses the XML to extract per-bug findings
- Converts to revio's Finding model with severity mapping (CONFIDENCE/RANK)

SpotBugs needs a Java environment and compiled class files (.class) or
a JAR. For raw .java sources it can't operate — those need compilation first.
This wrapper checks for class files at the target and skips with a clear
message otherwise. The agent can still review raw .java via the generic
AST tools and LLM reasoning.
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


class SpotBugsNotInstalledError(RuntimeError):
    """spotbugs binary not found.

    Install via:  brew install spotbugs  (macOS)
                  apt install spotbugs   (Debian/Ubuntu, if packaged)
                  # Or download from https://spotbugs.github.io/
    """


class SpotBugsExecutionError(RuntimeError):
    """spotbugs ran but produced unusable output."""


# --- Models ------------------------------------------------------------------


class SpotBugsLocation(BaseModel):
    classname: str = ""
    sourcefile: str = ""
    line_start: int = 0
    line_end: int = 0


class SpotBugsBug(BaseModel):
    type: str = ""              # e.g. "SQL_NONCONSTANT_STRING_PASSED_TO_EXECUTE"
    category: str = ""          # SECURITY | BAD_PRACTICE | CORRECTNESS | ...
    priority: int = 3           # 1 (most severe) ... 5
    rank: int = 20              # scariest 1-4, scary 5-9, troubling 10-14, of concern 15-20
    abbrev: str = ""            # e.g. "SQL"
    message: str = ""           # primary message
    long_message: str = ""      # extended explanation
    locations: list[SpotBugsLocation] = Field(default_factory=list)
    cwe: int | None = None

    @property
    def primary_location(self) -> SpotBugsLocation | None:
        return self.locations[0] if self.locations else None


# --- Severity / category mapping --------------------------------------------


def _map_severity(bug: SpotBugsBug) -> Severity:
    """Combine priority (1-5) + rank (1-20) into a revio severity."""
    # priority 1 == highest. Rank 1-4 == "scariest".
    if bug.priority <= 1 or bug.rank <= 4:
        return Severity.CRITICAL
    if bug.priority == 2 or bug.rank <= 9:
        return Severity.ERROR
    if bug.priority == 3 or bug.rank <= 14:
        return Severity.WARNING
    return Severity.INFO


def _map_category(bug: SpotBugsBug) -> ReviewCategory:
    cat = bug.category.upper()
    if cat == "SECURITY":
        return ReviewCategory.SECURITY
    if cat in {"CORRECTNESS", "MT_CORRECTNESS"}:
        return ReviewCategory.POTENTIAL_BUG
    if cat == "PERFORMANCE":
        return ReviewCategory.PERFORMANCE
    if cat in {"BAD_PRACTICE", "STYLE"}:
        return ReviewCategory.CODE_STYLE
    if cat == "EXPERIMENTAL":
        return ReviewCategory.POTENTIAL_BUG
    return ReviewCategory.POTENTIAL_BUG


# --- Runner ------------------------------------------------------------------


class SpotBugsRunner:
    """Run SpotBugs on a Java target and return structured findings."""

    DEFAULT_TIMEOUT_SECONDS = 300

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or self._locate_binary()
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS

    # ---- Public API ----

    def scan(self, target: Path | str) -> list[SpotBugsBug]:
        """Run spotbugs on a directory of class files or a jar; return bugs."""
        target = Path(target).resolve()
        if not target.exists():
            raise FileNotFoundError(f"target does not exist: {target}")

        # SpotBugs analyzes COMPILED class files. If there are none, bail.
        if not self._has_class_files(target):
            logger.info(
                "spotbugs: no .class files at %s — needs compiled output. "
                "Compile with `javac` or `mvn compile` first.", target
            )
            return []

        cmd = [
            self.binary, "-textui",
            "-xml:withMessages",
            "-quiet",
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
            raise SpotBugsExecutionError(
                f"spotbugs timed out after {self.timeout}s on {target}"
            ) from e

        if not proc.stdout.strip():
            stderr = (proc.stderr or "").strip()
            raise SpotBugsExecutionError(
                f"spotbugs produced no XML (exit={proc.returncode}, stderr={stderr[:300]})"
            )

        return self._parse_xml(proc.stdout)

    def scan_to_findings(
        self,
        target: Path | str,
        *,
        repo_root: Path | str | None = None,
    ) -> list[Finding]:
        bugs = self.scan(target)
        root = Path(repo_root).resolve() if repo_root else None
        return [self._to_finding(b, root) for b in bugs if b.primary_location]

    # ---- Internals ----

    @staticmethod
    def _has_class_files(target: Path) -> bool:
        if target.is_file() and (target.suffix == ".jar" or target.suffix == ".class"):
            return True
        if target.is_dir():
            for _ in target.rglob("*.class"):
                return True
        return False

    @staticmethod
    def _locate_binary() -> str:
        env_path = os.environ.get("REVIO_SPOTBUGS_BIN")
        if env_path and Path(env_path).is_file():
            return env_path

        # Common brew install location
        brew_path = Path("/opt/homebrew/bin/spotbugs")
        if brew_path.is_file():
            return str(brew_path)
        brew_path_intel = Path("/usr/local/bin/spotbugs")
        if brew_path_intel.is_file():
            return str(brew_path_intel)

        which = shutil.which("spotbugs")
        if which:
            return which

        raise SpotBugsNotInstalledError(
            "spotbugs not found. Install via:\n"
            "  brew install spotbugs    (macOS)\n"
            "or download from https://spotbugs.github.io/\n"
            "(Or set REVIO_SPOTBUGS_BIN)"
        )

    @staticmethod
    def _parse_xml(xml_text: str) -> list[SpotBugsBug]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise SpotBugsExecutionError(f"spotbugs XML parse failed: {e}") from e

        bugs: list[SpotBugsBug] = []
        for bug_inst in root.findall(".//BugInstance"):
            short_msg = (bug_inst.findtext("ShortMessage") or "").strip()
            long_msg = (bug_inst.findtext("LongMessage") or "").strip()
            try:
                cwe_attr = bug_inst.get("cweid")
                cwe = int(cwe_attr) if cwe_attr else None
            except ValueError:
                cwe = None

            locations: list[SpotBugsLocation] = []
            for loc_el in bug_inst.findall("SourceLine"):
                try:
                    line_start = int(loc_el.get("start") or 0)
                    line_end = int(loc_el.get("end") or 0)
                except ValueError:
                    line_start = line_end = 0
                locations.append(SpotBugsLocation(
                    classname=loc_el.get("classname", "") or "",
                    sourcefile=loc_el.get("sourcefile", "") or "",
                    line_start=line_start,
                    line_end=line_end,
                ))

            try:
                priority = int(bug_inst.get("priority") or 3)
            except ValueError:
                priority = 3
            try:
                rank = int(bug_inst.get("rank") or 20)
            except ValueError:
                rank = 20

            bugs.append(SpotBugsBug(
                type=bug_inst.get("type") or "",
                category=bug_inst.get("category") or "",
                priority=priority,
                rank=rank,
                abbrev=bug_inst.get("abbrev") or "",
                message=short_msg,
                long_message=long_msg,
                locations=locations,
                cwe=cwe,
            ))

        return bugs

    @staticmethod
    def _to_finding(bug: SpotBugsBug, repo_root: Path | None) -> Finding:
        loc = bug.primary_location
        assert loc is not None  # caller filters

        # spotbugs gives a relative sourcefile (e.g. "Greeter.java")
        # plus a classname (e.g. "com.example.Greeter"). Reconstruct best-effort.
        file_path = loc.sourcefile
        if repo_root:
            # Try to find the file under repo_root by name
            matches = list(repo_root.rglob(loc.sourcefile))
            if matches:
                try:
                    file_path = str(matches[0].relative_to(repo_root))
                except ValueError:
                    file_path = matches[0].name

        evidence: list[Evidence] = [
            Evidence(
                kind="static_rule",
                summary=f"spotbugs {bug.type}: {bug.message}",
                source=f"spotbugs:{bug.type}",
            )
        ]
        if bug.long_message and bug.long_message != bug.message:
            evidence.append(Evidence(
                kind="reasoning",
                summary=bug.long_message[:200],
                detail=bug.long_message,
                source="spotbugs:explanation",
            ))
        if bug.cwe:
            evidence.append(Evidence(
                kind="cross_reference",
                summary=f"CWE-{bug.cwe}",
                source="cwe",
            ))

        title = bug.message
        if len(title) > 80:
            title = title[:77] + "..."

        return Finding(
            file_path=file_path,
            line_start=loc.line_start or 1,
            line_end=loc.line_end if loc.line_end > loc.line_start else None,
            severity=_map_severity(bug),
            category=_map_category(bug),
            title=title,
            hypothesis=f"spotbugs '{bug.type}' triggered: {bug.message}",
            evidence=evidence,
            confidence=0.85 if bug.priority <= 2 else 0.7,
            verified=True,
            suggestion=bug.long_message if bug.long_message else None,
            detected_by="static",
        )
