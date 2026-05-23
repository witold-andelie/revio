"""PLC profile tools.

Wires the ported v1 PLC capability stack into agent-callable tools:
- parse_plc_file: route a file through vendor parser chain
- extract_plc_source: pull ST source from any supported PLC artifact
- run_plc_rules: 30+ PLCopen / Secure-PLC coding rules
- run_plc_cfg_analysis: control-flow graph analysis (dead store, unreachable)
- run_plc_hw_audit: TIA Portal HWConfig safety/security audit
- convert_graphical_to_st: LD/FBD/SFC → ST helper
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.tools import tool

from ..output.models import Evidence, Finding, ReviewCategory, Severity
from .generic_tools import make_generic_ast_tools  # Tree-sitter fallback only; PLC lang not bundled
from .tool_context import ToolContext


logger = logging.getLogger(__name__)


# --- Severity mapping (PLC severity strings → revio Severity) ---------------


_PLC_SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "error":    Severity.ERROR,
    "warning":  Severity.WARNING,
    "info":     Severity.INFO,
}


_PLC_RULE_CATEGORY = {
    # Direct-mapping rule prefixes
    "PLC-007": ReviewCategory.SECURITY,        # output without interlock
    "PLC-008": ReviewCategory.POTENTIAL_BUG,   # race condition
    "PLC-009": ReviewCategory.SECURITY,        # E-stop missing
    "PLC-017": ReviewCategory.SECURITY,        # sensor validation missing
    "PLC-018": ReviewCategory.SECURITY,        # comm timeout missing
    "PLC-019": ReviewCategory.SECURITY,        # fail-safe default
    "PLC-002": ReviewCategory.SECURITY,        # watchdog
    "PLC-006": ReviewCategory.POTENTIAL_BUG,   # division by zero
    "PLC-012": ReviewCategory.POTENTIAL_BUG,   # pointer deref
    # CFG findings
    "CFG-001": ReviewCategory.POTENTIAL_BUG,
    "CFG-002": ReviewCategory.POTENTIAL_BUG,
    "CFG-003": ReviewCategory.POTENTIAL_BUG,
    "CFG-004": ReviewCategory.REDUNDANCY,
    "CFG-005": ReviewCategory.READABILITY,
    # HW config
    "HW-001": ReviewCategory.SECURITY,
    "HW-002": ReviewCategory.SECURITY,
    "HW-005": ReviewCategory.SECURITY,
    "HW-006": ReviewCategory.SECURITY,
    "HW-007": ReviewCategory.SECURITY,
    "HW-008": ReviewCategory.SECURITY,
    "HW-009": ReviewCategory.SECURITY,
}


def _plc_violation_to_finding(violation, file_path: str) -> Finding:
    """Convert a PLCRuleViolation to revio's Finding model."""
    sev = _PLC_SEVERITY_MAP.get(violation.severity, Severity.WARNING)
    cat = _PLC_RULE_CATEGORY.get(violation.rule_id, ReviewCategory.CONVENTION)

    evidence = [
        Evidence(
            kind="static_rule",
            summary=f"{violation.rule_id} ({violation.rule_name}): {violation.description}",
            source=f"plc:{violation.rule_id}",
        )
    ]
    if violation.suggestion:
        evidence.append(Evidence(
            kind="reasoning",
            summary=violation.suggestion[:200],
            detail=violation.suggestion,
            source="plc:suggestion",
        ))

    title = violation.rule_name
    if len(title) > 80:
        title = title[:77] + "..."

    return Finding(
        file_path=file_path,
        line_start=violation.line_number or 1,
        severity=sev,
        category=cat,
        title=title,
        hypothesis=f"PLC rule '{violation.rule_id}' triggered: {violation.description}",
        evidence=evidence,
        confidence=0.9,
        verified=True,
        suggestion=violation.suggestion,
        detected_by="static",
    )


# --- Tool factories ----------------------------------------------------------


def make_parse_plc_file_tool(ctx: ToolContext):
    @tool
    def parse_plc_file(relative_path: str) -> str:
        """Parse a PLC artifact (XML/L5X/.smc2) and report what's inside.

        Walks the vendor parser chain (SimaticML → TwinCAT → CODESYS → Rockwell
        → ABB → GE → Omron → generic XML) and reports format + block/POU info.

        Args:
            relative_path: PLC file under the repo root.

        Returns:
            Format detected + POU info or a "not a PLC file" message.
        """
        from ..layers.parser.plc import is_plc_project_file
        from ..layers.parser.plc.simatic import SimaticMLParser
        from ..layers.parser.plc.twincat import TwincatParser
        from ..layers.parser.plc.codesys import CodesysParser
        from ..layers.parser.plc.rockwell import RockwellParser
        from ..layers.parser.plc.abb import ABBParser
        from ..layers.parser.plc.ge import GEParser
        from ..layers.parser.plc.omron import OmronParser

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."
        if not full.is_file():
            return f"Error: not a file: {relative_path}"

        if not is_plc_project_file(str(full)):
            return f"'{relative_path}' is not a recognised PLC artifact."

        # Try each vendor in order, report whichever matched
        checks = [
            ("Siemens TIA Portal (SimaticML)", SimaticMLParser.is_simaticml),
            ("Beckhoff TwinCAT 3 (TcPOU)", TwincatParser.is_twincat),
            ("CODESYS V3", CodesysParser.is_codesys),
            ("Rockwell Studio 5000 (L5X)", RockwellParser.is_l5x),
            ("ABB Automation Builder", ABBParser.is_abb),
            ("GE/Fanuc Proficy", GEParser.is_ge),
            ("Omron Sysmac Studio", OmronParser.is_omron),
        ]
        for vendor, detector in checks:
            try:
                if detector(str(full)):
                    return f"{relative_path}: detected as {vendor}"
            except Exception:
                continue
        return f"{relative_path}: PLC XML (vendor not specifically recognized)"

    return parse_plc_file


def make_extract_plc_source_tool(ctx: ToolContext):
    @tool
    def extract_plc_source(relative_path: str, max_lines: int = 400) -> str:
        """Extract Structured Text source from a PLC artifact.

        Routes through the vendor parser chain; graphical-language sections
        (LD/FBD/SFC) are converted to ST inline.

        Args:
            relative_path: PLC file under repo root.
            max_lines: Truncate output if longer (default 400).

        Returns:
            The extracted ST source (numbered) or an error message.
        """
        from ..layers.parser.plc import extract_structured_text
        from ..layers.parser.plc.converters import (
            FBDConverter, LadderDiagramConverter, SFCConverter
        )

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."
        if not full.is_file():
            return f"Error: not a file: {relative_path}"

        # Plain .st / .iecst files — read directly
        suffix = full.suffix.lower()
        if suffix in {".st", ".iecst"}:
            try:
                source = full.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                return f"Error reading file: {e}"
        else:
            source = extract_structured_text(str(full))
            if source is None:
                return f"Could not extract ST from {relative_path} (vendor not recognized)"

        # Convert any graphical-language sections to ST
        for conv in (FBDConverter, LadderDiagramConverter, SFCConverter):
            try:
                marker_fn = (
                    conv.has_fbd_marker if conv is FBDConverter
                    else conv.has_graphical_language if conv is LadderDiagramConverter
                    else conv.has_sfc_marker
                )
                if marker_fn(source):
                    result = conv.extract_and_convert(source)
                    if result.st_code:
                        source += f"\n\n// === {conv.__name__} conversion ===\n{result.st_code}"
            except Exception as e:
                logger.debug("converter %s failed: %s", conv.__name__, e)

        lines = source.splitlines()
        total = len(lines)
        if total > max_lines:
            lines = lines[:max_lines]
            trailer = f"\n... ({total - max_lines} more lines)"
        else:
            trailer = ""
        numbered = "\n".join(f"{i+1:5d}  {line}" for i, line in enumerate(lines))
        return f"# {relative_path} ({total} lines, lang=PLC ST)\n{numbered}{trailer}"

    return extract_plc_source


def make_run_plc_rules_tool(ctx: ToolContext):
    @tool
    def run_plc_rules(relative_path: str) -> str:
        """Run the 30+ PLCopen / Secure-PLC coding rules on a Structured Text file.

        Covers pattern (PLC-001..PLC-012), structural (PLC-013..PLC-024),
        and semantic (PLC-007/008/009/017/018/019) rules.

        Args:
            relative_path: PLC file (.st/.iecst/.xml/.l5x/.smc2) under repo root.

        Returns:
            Findings list (auto-recorded — no need to call report_finding).
        """
        from ..layers.parser.plc import extract_structured_text
        from ..layers.static.plc_rules import PLCRulesChecker

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."
        if not full.is_file():
            return f"Error: not a file: {relative_path}"

        suffix = full.suffix.lower()
        if suffix in {".st", ".iecst"}:
            source = full.read_text(encoding="utf-8", errors="ignore")
        else:
            source = extract_structured_text(str(full))
            if source is None:
                return f"Could not extract ST from {relative_path} (vendor not recognized)"

        try:
            violations = PLCRulesChecker.check_code(source)
        except Exception as e:
            return f"Error running PLC rules: {e}"

        if not violations:
            return f"(no PLC rule violations in {relative_path})"

        findings = [_plc_violation_to_finding(v, relative_path) for v in violations]
        ctx.pending_findings.extend(findings)

        max_show = 50
        lines = [
            f"PLC rule findings in {relative_path} ({len(violations)} total — "
            f"auto-recorded, no need to call report_finding for these):"
        ]
        for v in violations[:max_show]:
            line_str = f"L{v.line_number}" if v.line_number else "(no line)"
            lines.append(f"  [{v.severity:8}] {v.rule_id}/{v.rule_name} {line_str}")
        if len(violations) > max_show:
            lines.append(f"  ... ({len(violations) - max_show} more)")
        return "\n".join(lines)

    return run_plc_rules


def make_run_plc_cfg_analysis_tool(ctx: ToolContext):
    @tool
    def run_plc_cfg_analysis(relative_path: str) -> str:
        """Run control-flow graph analysis on a PLC ST file.

        Detects: unreachable code (CFG-001), infinite loops (CFG-002),
        use-before-define (CFG-003), dead stores (CFG-004), high
        cyclomatic complexity (CFG-005).

        Args:
            relative_path: PLC file under repo root.

        Returns:
            CFG findings (auto-recorded).
        """
        from ..layers.parser.plc import extract_structured_text
        from ..layers.static.plc_cfg import CFGAnalyzer

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."
        if not full.is_file():
            return f"Error: not a file: {relative_path}"

        suffix = full.suffix.lower()
        if suffix in {".st", ".iecst"}:
            source = full.read_text(encoding="utf-8", errors="ignore")
        else:
            source = extract_structured_text(str(full))
            if source is None:
                return f"Could not extract ST from {relative_path}"

        try:
            cfg_findings = CFGAnalyzer.analyze(source)
        except Exception as e:
            return f"Error in CFG analysis: {e}"

        if not cfg_findings:
            return f"(no CFG findings in {relative_path})"

        # CFG analyzer returns dicts, not PLCRuleViolation. Convert directly.
        findings = []
        for cf in cfg_findings:
            sev = _PLC_SEVERITY_MAP.get(cf.get("severity", "warning"), Severity.WARNING)
            rule_id = cf.get("rule_id", "CFG-???")
            cat = _PLC_RULE_CATEGORY.get(rule_id, ReviewCategory.POTENTIAL_BUG)
            f = Finding(
                file_path=relative_path,
                line_start=cf.get("line_number") or 1,
                severity=sev,
                category=cat,
                title=cf.get("rule_name", "CFG finding")[:80],
                hypothesis=f"CFG analysis: {cf.get('description', '')}",
                evidence=[Evidence(
                    kind="static_rule",
                    summary=f"{rule_id}: {cf.get('description', '')}",
                    source=f"plc_cfg:{rule_id}",
                )],
                confidence=0.85,
                verified=True,
                detected_by="static",
            )
            findings.append(f)
        ctx.pending_findings.extend(findings)

        lines = [
            f"CFG findings in {relative_path} ({len(cfg_findings)} total — "
            f"auto-recorded):"
        ]
        for cf in cfg_findings[:30]:
            line_str = f"L{cf.get('line_number')}" if cf.get('line_number') else "(no line)"
            lines.append(
                f"  [{cf.get('severity'):8}] {cf.get('rule_id')} {line_str}: "
                f"{cf.get('description', '')[:60]}"
            )
        return "\n".join(lines)

    return run_plc_cfg_analysis


def make_run_plc_hw_audit_tool(ctx: ToolContext):
    @tool
    def run_plc_hw_audit(relative_path: str) -> str:
        """Audit a TIA Portal hardware configuration XML for safety/security issues.

        12 rules: vulnerable firmware (HW-001), low protection level (HW-002),
        watchdog misconfig (HW-003), unprotected safety I/O (HW-004), safety
        CPU without safety program (HW-005), PROFINET without port security
        (HW-006), safety CPU without password (HW-007), insecure web server
        (HW-008), unencrypted S7comm (HW-009), watchdog mismatch (HW-010),
        low CPU memory (HW-011), article-number/model mismatch (HW-012).

        Args:
            relative_path: TIA Portal HWConfig XML under repo root.

        Returns:
            HW findings (auto-recorded).
        """
        from ..layers.static.plc_hw_config import HWConfigParser, HWConfigRulesChecker

        full = (ctx.repo_root / relative_path).resolve()
        try:
            full.relative_to(ctx.repo_root)
        except ValueError:
            return f"Error: '{relative_path}' is outside the repo."
        if not full.is_file():
            return f"Error: not a file: {relative_path}"

        if not HWConfigParser.is_hwconfig(str(full)):
            return f"{relative_path} is not a TIA Portal HWConfig XML."

        try:
            config = HWConfigParser.parse_file(str(full))
            if config is None:
                return f"Could not parse HWConfig at {relative_path}"
            violations = HWConfigRulesChecker.check(config)
        except Exception as e:
            return f"Error in HW config audit: {e}"

        if not violations:
            return f"(no HW config violations in {relative_path})"

        findings = []
        for v in violations:
            sev = _PLC_SEVERITY_MAP.get(v.severity, Severity.WARNING)
            cat = _PLC_RULE_CATEGORY.get(v.rule_id, ReviewCategory.SECURITY)
            findings.append(Finding(
                file_path=relative_path,
                line_start=1,
                severity=sev,
                category=cat,
                title=f"[{v.rule_id}] {v.rule_name}"[:80],
                hypothesis=f"[{v.component}] {v.description}",
                evidence=[Evidence(
                    kind="static_rule",
                    summary=f"{v.rule_id}: {v.description}",
                    source=f"plc_hw:{v.rule_id}",
                )],
                confidence=0.95,
                verified=True,
                suggestion=v.suggestion,
                detected_by="static",
            ))
        ctx.pending_findings.extend(findings)

        lines = [
            f"HW config findings in {relative_path} ({len(violations)} total — "
            f"auto-recorded):"
        ]
        for v in violations[:30]:
            lines.append(
                f"  [{v.severity:8}] {v.rule_id}/{v.rule_name}: {v.description[:60]}"
            )
        return "\n".join(lines)

    return run_plc_hw_audit


# --- Bundle ------------------------------------------------------------------


def make_plc_tools(ctx: ToolContext) -> list:
    """All PLC profile tools."""
    return [
        make_parse_plc_file_tool(ctx),
        make_extract_plc_source_tool(ctx),
        make_run_plc_rules_tool(ctx),
        make_run_plc_cfg_analysis_tool(ctx),
        make_run_plc_hw_audit_tool(ctx),
    ]
