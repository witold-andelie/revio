"""PLC profile — IEC 61131-3 Structured Text + vendor XML formats.

M1: declarative stub only. Real implementation reuses v1's PLCRulesChecker,
CFGAnalyzer, and 7 vendor parsers — ported and bug-fixed in M4.
"""

from ..base import ProfileBase, register


@register("plc")
class PLCProfile(ProfileBase):
    description = "PLC: IEC 61131-3 ST + 7 vendor XML formats"
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
        )
