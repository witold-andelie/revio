"""Verilog / SystemVerilog profile — LLM-only review."""

from ..base import ProfileBase, register


@register("verilog")
class VerilogProfile(ProfileBase):
    description = "Verilog / SystemVerilog (LLM-only review)"
    extensions = (".v", ".vh", ".sv", ".svh", ".vhd", ".vhdl")
    languages = ("verilog", "systemverilog", "vhdl")

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Verilog / SystemVerilog / VHDL (HDL).\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for (HDL-specific):\n"
            "- Combinational latches inferred from incomplete always blocks\n"
            "  (always @(*) missing else / default — synthesis adds an implicit latch)\n"
            "- Race conditions: blocking (=) vs non-blocking (<=) assignments mixed\n"
            "  in the same always block. Use <= in sequential, = in combinational.\n"
            "- Sensitivity list incomplete: always @(a) when block uses b too\n"
            "- Clock domain crossing without synchronizer chain (metastability)\n"
            "- Asynchronous reset without synchronous deassert\n"
            "- Multiply-driven nets (X states in simulation)\n"
            "- Wire used where reg required (or vice versa)\n"
            "- Inferred memory vs flip-flops: large reg arrays in synthesizable code\n"
            "- Missing default in case statements (latch inference)\n"
            "- $display / $monitor left in synthesizable code (synthesis warnings)\n"
            "- Unsized constants: `'1` vs `1'b1` — behavior differs across sim/synth\n"
            "- Parameter overrides not propagated to all instantiations\n"
            "- Timing: gate-level delays in RTL code (#5 should not exist in synth code)\n"
            "- Out-of-range bit selects yielding X without warning\n"
            "- Generate blocks: missing label causes synthesis confusion\n"
            "- Async FIFOs: full/empty flags computed in wrong clock domain\n"
            "- For SystemVerilog: virtual interfaces accessed across module boundaries\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
