"""PLC profile end-to-end smoke test (M4).

Verifies the ported v1 PLC stack:
- Parser core: file_support, st_extractor, xml_parser
- 7 vendor parsers + generic XML fallback (import-only — real XML fixtures
  would require vendor-specific tooling to generate)
- Layer 2: plc_rules (30+ rules across 3 levels), plc_cfg (CFG analysis),
  plc_hw_config (TIA Portal HW audit)
- 3 graphical converters (LD/FBD/SFC → ST)
- Profile registration + tool wiring

Uses tests/fixtures/plc_sample/MotorControl.st — a deliberately violation-
packed ST program covering PLC-001/006/007/009/013/014/016/017/019/020.

Run:
    .venv/bin/python tests/test_plc_smoke.py
"""

from __future__ import annotations

from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "plc_sample"


def _section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def check_parser_imports() -> int:
    _section("PLC parser core + vendor parsers (imports)")
    # Core
    from revio.layers.parser.plc import (
        STFunctionBlock,
        STVariable,
        StructuredTextExtractor,
        extract_structured_text,
        has_plc_project_extension,
        is_plc_project_file,
    )
    # Vendor parsers
    from revio.layers.parser.plc.abb import ABBParser
    from revio.layers.parser.plc.codesys import CodesysParser
    from revio.layers.parser.plc.ge import GEParser
    from revio.layers.parser.plc.omron import OmronParser
    from revio.layers.parser.plc.rockwell import RockwellParser
    from revio.layers.parser.plc.simatic import SimaticMLParser
    from revio.layers.parser.plc.twincat import TwincatParser
    from revio.layers.parser.plc.xml_parser import PLCXmlParser
    # Converters
    from revio.layers.parser.plc.converters import (
        FBDConverter,
        LadderDiagramConverter,
        SFCConverter,
    )
    # Static
    from revio.layers.static.plc_rules import PLCRulesChecker, PLCRuleViolation
    from revio.layers.static.plc_cfg import CFGAnalyzer
    from revio.layers.static.plc_hw_config import HWConfigParser, HWConfigRulesChecker

    print("  ✓ all PLC modules import cleanly")
    return 0


def check_field_naming() -> int:
    _section("STVariable.datatype field consistency")
    from revio.layers.parser.plc import STVariable, StructuredTextExtractor

    source = """
VAR_INPUT
    Speed : REAL := 100.0;
    Count : INT;
END_VAR
"""
    variables = StructuredTextExtractor.extract_variables(source)
    if len(variables) != 2:
        print(f"  ❌ Expected 2 variables, got {len(variables)}")
        return 1

    # Most important check: 'datatype' field (NOT data_type) — the v1 bug fix
    for v in variables:
        if not hasattr(v, "datatype"):
            print(f"  ❌ STVariable.datatype missing (v1's data_type bug not fixed)")
            return 1
        if hasattr(v, "data_type"):
            print(f"  ❌ Old 'data_type' field still present (should be removed)")
            return 1

    speed = next(v for v in variables if v.name == "Speed")
    if speed.datatype != "REAL":
        print(f"  ❌ Speed.datatype = {speed.datatype!r}, expected 'REAL'")
        return 1
    if speed.initial_value != "100.0":
        print(f"  ❌ Speed.initial_value = {speed.initial_value!r}")
        return 1

    print("  ✓ STVariable uses 'datatype' field correctly (v1 inconsistency fixed)")
    return 0


def check_plc_rules() -> int:
    _section("PLC rules checker (30+ rules, 3 levels)")
    from revio.layers.static.plc_rules import PLCRulesChecker

    path = FIXTURE / "MotorControl.st"
    if not path.is_file():
        print(f"  ❌ fixture missing: {path}")
        return 1

    source = path.read_text(encoding="utf-8")
    violations = PLCRulesChecker.check_code(source)
    print(f"  Rule violations: {len(violations)}")

    if len(violations) < 10:
        print(f"  ❌ Expected ≥ 10 violations from the deliberately bad fixture, got {len(violations)}")
        return 1

    rule_ids = {v.rule_id for v in violations}
    expected_rules = {
        "PLC-001",  # direct I/O
        "PLC-003",  # magic number
        "PLC-006",  # division (M4 regex fix)
        "PLC-020",  # GOTO
        "PLC-002",  # missing watchdog
        "PLC-009",  # missing E-stop
    }
    missing = expected_rules - rule_ids
    if missing:
        print(f"  ❌ Expected rules not fired: {missing}")
        return 1
    print(f"  ✓ all expected rule IDs triggered: {sorted(expected_rules)}")
    print(f"  ✓ total {len(rule_ids)} distinct rules fired")
    return 0


def check_plc_006_fix() -> int:
    _section("PLC-006 division regex (M4 false-positive fix)")
    from revio.layers.static.plc_rules import PLCRulesChecker

    # File path inside a string — v1's old regex would falsely flag this as
    # division because of the '/'. The M4 fix should NOT report PLC-006 here.
    safe_code = """
PROGRAM Main
VAR
    config_path : STRING := '/etc/plc/config.xml';
END_VAR
END_PROGRAM
"""
    violations = PLCRulesChecker.check_code(safe_code)
    plc006_hits = [v for v in violations if v.rule_id == "PLC-006"]
    if plc006_hits:
        print(f"  ❌ PLC-006 false-positive on path-in-string: {plc006_hits}")
        return 1
    print(f"  ✓ no PLC-006 false positives on file paths inside strings")

    # Real division must STILL fire
    bad_code = """
PROGRAM Main
VAR
    Speed : REAL;
    Divisor : REAL;
    Result : REAL;
END_VAR
    Result := Speed / Divisor;
END_PROGRAM
"""
    violations = PLCRulesChecker.check_code(bad_code)
    plc006_hits = [v for v in violations if v.rule_id == "PLC-006"]
    if not plc006_hits:
        print(f"  ❌ PLC-006 did not fire on actual division")
        return 1
    print(f"  ✓ PLC-006 still fires on real division (positive case preserved)")
    return 0


def check_profile_wiring() -> int:
    _section("PLC profile registration + tool wiring")
    from pathlib import Path

    from revio.agent.tool_context import ToolContext
    from revio.profiles import get_profile, load_all_profiles

    load_all_profiles()
    plc = get_profile("plc")
    if plc is None:
        print("  ❌ PLC profile not registered")
        return 1
    if "30 PLCopen rules" not in plc.description and "PLCopen" not in plc.description:
        print(f"  ⚠ description doesn't mention rule count (got: {plc.description})")

    ctx = ToolContext(repo_root=FIXTURE, profile_name="plc")
    tools = plc.make_tools(ctx)
    tool_names = {t.name for t in tools}
    expected_tools = {
        "parse_plc_file",
        "extract_plc_source",
        "run_plc_rules",
        "run_plc_cfg_analysis",
        "run_plc_hw_audit",
    }
    missing = expected_tools - tool_names
    if missing:
        print(f"  ❌ missing PLC tools: {missing}")
        return 1
    print(f"  ✓ all 5 PLC tools registered: {sorted(tool_names)}")
    return 0


def check_run_plc_rules_tool() -> int:
    _section("run_plc_rules tool — auto-emit findings to ctx")
    from pathlib import Path

    from revio.agent.tool_context import ToolContext
    from revio.profiles import get_profile, load_all_profiles

    load_all_profiles()
    ctx = ToolContext(repo_root=FIXTURE, profile_name="plc")
    tools_by_name = {t.name: t for t in get_profile("plc").make_tools(ctx)}

    result = tools_by_name["run_plc_rules"].invoke({"relative_path": "MotorControl.st"})
    if "auto-recorded" not in result:
        print("  ❌ tool output should mention auto-recorded")
        return 1

    if len(ctx.pending_findings) < 10:
        print(f"  ❌ expected ≥ 10 pending findings, got {len(ctx.pending_findings)}")
        return 1
    print(f"  ✓ run_plc_rules pushed {len(ctx.pending_findings)} findings to ctx.pending_findings")
    for f in ctx.pending_findings[:3]:
        print(f"    · [{f.severity.value}] {f.title[:50]}  ({f.file_path}:{f.line_start})")
    print(f"    ... ({len(ctx.pending_findings) - 3} more)")
    return 0


def main() -> int:
    print("=" * 70)
    print("PLC profile smoke test (M4)")
    print("=" * 70)

    rc = 0
    rc |= check_parser_imports()
    rc |= check_field_naming()
    rc |= check_plc_rules()
    rc |= check_plc_006_fix()
    rc |= check_profile_wiring()
    rc |= check_run_plc_rules_tool()

    print()
    if rc == 0:
        print("✓ ALL PLC SMOKE CHECKS PASSED")
    else:
        print("❌ Some PLC checks failed")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
