"""Verilog / SystemVerilog profile — Tree-sitter AST + verilator lint.

VHDL is intentionally NOT included — it has a different grammar and a
different tool ecosystem; we leave it to the LLM-only path via the
detect-fingerprint fallback.
"""

from ..base import ProfileBase, register


@register("verilog")
class VerilogProfile(ProfileBase):
    description = "Verilog / SystemVerilog (Tree-sitter AST + verilator)"
    extensions = (".v", ".vh", ".sv", ".svh")
    languages = ("verilog", "systemverilog")
    optional_dep_group = "languages"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Verilog / SystemVerilog (HDL).\n"
            "Common issue patterns to watch for (HDL-specific):\n"
            "- Combinational latches inferred from incomplete always blocks\n"
            "  (always @(*) missing else / default in case)\n"
            "- Race conditions: blocking (=) vs non-blocking (<=) assignment\n"
            "  mixed in the same always block. Use <= in sequential, = in combinational.\n"
            "- Sensitivity list incomplete: always @(a) when block uses b too\n"
            "- Clock domain crossing without synchronizer chain (metastability)\n"
            "- Asynchronous reset without synchronous deassert\n"
            "- Multiply-driven nets (X states in simulation)\n"
            "- Bit-width mismatches in assignment / port connection (truncation /\n"
            "  zero-extension silently)\n"
            "- Wire used where reg required (or vice versa)\n"
            "- Inferred memory vs flip-flops: large reg arrays in synthesizable code\n"
            "- Missing default in case statements (latch inference)\n"
            "- $display / $monitor / `assert` left in synthesizable code\n"
            "- Cross-module-reference (XMR) used outside of testbench\n"
            "\n"
            "Specialized tools available:\n"
            "- run_verilator: --lint-only pass for synthesis-correctness issues\n"
            "  (call first — auto-emits findings for WIDTH/LATCH/MULTIDRIVEN/etc.)\n"
            "- get_function_at / list_functions / list_classes:\n"
            "  Tree-sitter AST queries (modules treated as class-like)\n"
        )

    @classmethod
    def make_tools(cls, ctx) -> list:
        from ..generic_runtime import make_generic_tools_for_profile
        from ...agent.lint_tools import make_verilator_tool

        return list(make_generic_tools_for_profile(ctx)) + [make_verilator_tool(ctx)]
