"""PLC profile — IEC 61131-3 Structured Text + 7 vendor XML formats.

M4: full Layer 1 + Layer 2 implementation, ported from v1's intelligent-
code-review-agent. Replaces M1's declarative stub.
"""

from ..base import ProfileBase, register


@register("plc")
class PLCProfile(ProfileBase):
    description = "PLC: IEC 61131-3 ST + 7 vendor XML + 30 PLCopen rules + HW audit"
    extensions = (".st", ".iecst", ".l5x", ".smc2", ".xml")
    languages = ("structured_text",)
    optional_dep_group = "plc"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: IEC 61131-3 Structured Text (PLC code).\n"
            "Safety-critical context — be alert to:\n"
            "- Missing emergency stop / E-Stop handling on physical outputs\n"
            "- Division without divisor zero check (runtime fault)\n"
            "- Floating-point equality comparison (precision unreliable)\n"
            "- Array access without bounds check\n"
            "- Output writes without interlock conditions\n"
            "- Communication operations without timeout / heartbeat\n"
            "- Outputs without fail-safe default initial value\n"
            "- Direct I/O addresses (e.g. %I0.0) in program body (portability)\n"
            "- Missing watchdog timer in cyclic programs\n"
            "- Race conditions: multiple unconditional writes to same output\n"
            "\n"
            "Specialized tools available:\n"
            "- parse_plc_file: detect vendor format (SimaticML / TwinCAT / CODESYS /\n"
            "  Rockwell L5X / ABB / GE / Omron) — call this FIRST on vendor XML files\n"
            "- extract_plc_source: pull ST source from any supported PLC artifact,\n"
            "  auto-converting LD/FBD/SFC graphical sections to ST\n"
            "- run_plc_rules: 30+ PLCopen + Secure-PLC coding rules (3 levels)\n"
            "- run_plc_cfg_analysis: control-flow graph analysis (dead store,\n"
            "  unreachable code, def-use, cyclomatic complexity)\n"
            "- run_plc_hw_audit: TIA Portal HWConfig XML — 12 hardware safety/\n"
            "  security rules (vulnerable firmware, weak protection, safety CPU\n"
            "  without password, unencrypted PROFINET, etc.)\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..plc_runtime import make_plc_tools_for_profile

        return make_plc_tools_for_profile(ctx)
