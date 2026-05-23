"""MATLAB profile — LLM-only review (no Tree-sitter grammar packaged on PyPI).

We provide rich reasoning hints so the LLM applies MATLAB-specific judgment.
Universal tools (read_file / list_files / search_guidelines / report_finding)
remain available — the agent reviews via direct reading + LLM reasoning.
"""

from ..base import ProfileBase, register


@register("matlab")
class MATLABProfile(ProfileBase):
    description = "MATLAB / Octave (LLM-only review)"
    extensions = (".m", ".mat")
    languages = ("matlab",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: MATLAB / Octave.\n"
            "MATLAB has no Tree-sitter grammar available on PyPI in revio's bundle,\n"
            "so you'll be reviewing via read_file + your trained knowledge.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- eval / evalin / feval on user-controlled strings (code injection)\n"
            "- str2num (executes input as expression — use str2double for numbers)\n"
            "- system / dos / unix on user input (command injection)\n"
            "- Hardcoded paths assuming Windows OR Linux (portability)\n"
            "- Implicit type conversions: 1 + 'a' silently yields 98 (ASCII math)\n"
            "- Memory: appending to arrays in loops without preallocation (O(n²))\n"
            "- Inefficient: looping over matrix elements vs vectorized operations\n"
            "- Mixed 0/1 indexing assumptions when porting from Python/C\n"
            "- Floating-point equality: == on doubles instead of abs(diff) < eps\n"
            "- Side effects in functions: global variables, persistent variables\n"
            "- Toolbox license dependencies that may not be present in all environments\n"
            "- Numerical: catastrophic cancellation, condition number not checked\n"
            "- Plot windows / figure handles left open (resource leak in batch jobs)\n"
            "\n"
            "No specialized static tools available for MATLAB in this profile —\n"
            "rely on read_file + your domain knowledge + search_guidelines.\n"
        )
