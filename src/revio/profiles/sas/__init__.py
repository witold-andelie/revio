"""SAS profile — LLM-only review."""

from ..base import ProfileBase, register


@register("sas")
class SASProfile(ProfileBase):
    description = "SAS (LLM-only review)"
    extensions = (".sas",)
    languages = ("sas",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: SAS (statistical / data-warehouse legacy).\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- SQL injection via macro variables in PROC SQL (no parameterization)\n"
            "- %SYSEXEC / %SYSCALL on user-controlled macro variables (cmd injection)\n"
            "- Hardcoded credentials in libname / odbc connection strings\n"
            "- Hardcoded file paths preventing portability across environments\n"
            "- Macro resolution order: &VAR vs &&VAR&i indirection subtleties\n"
            "- Missing OBS= / FIRSTOBS= on production data steps (memory/time)\n"
            "- WHERE vs IF: WHERE pushed to dataset, IF filters in-memory — wrong choice\n"
            "  yields huge perf regressions\n"
            "- Implicit type conversion via 'BEST.' format hiding precision loss\n"
            "- DROP / KEEP in dataset options not retaining variables for downstream steps\n"
            "- BY-group processing without prior PROC SORT (BY-statement order error)\n"
            "- Missing default-statement in data step DO loops (infinite loop on bad input)\n"
            "- ODBC / connection strings exposing passwords in SAS logs\n"
            "- LET _SQL_OPTS_=... that disable SQL injection protections\n"
            "- Lack of error handling: &SYSERR / &SQLRC / &SYSCC not checked\n"
            "- PUT statement leaking PII to logs in production batches\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
