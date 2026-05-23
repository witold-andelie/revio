"""COBOL profile — LLM-only review."""

from ..base import ProfileBase, register


@register("cobol")
class COBOLProfile(ProfileBase):
    description = "COBOL (LLM-only review)"
    extensions = (".cob", ".cbl", ".cpy")
    languages = ("cobol",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: COBOL.\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- Buffer overruns: MOVE on FILLER fields longer than receiving area\n"
            "- ON SIZE ERROR clause missing on COMPUTE / ADD that can overflow\n"
            "- File handles not closed on abend (CLOSE in declaratives missing)\n"
            "- Hardcoded JCL DD names assuming a specific environment\n"
            "- EXEC SQL: no host-variable boundaries, SQLCA codes not checked\n"
            "- DB2: dynamic SQL via EXEC SQL PREPARE without parameterized markers\n"
            "- CICS: missing RESP code check after EXEC CICS commands\n"
            "- Y2K-style date logic: 2-digit years still embedded in legacy code\n"
            "- File status code (FS) not checked after I/O verbs\n"
            "- PERFORM with no GO TO causing implicit fall-through bugs\n"
            "- GOBACK vs EXIT PROGRAM vs STOP RUN — wrong choice corrupts call chain\n"
            "- Numeric REDEFINES: alphanumeric data viewed as numeric → S0C7 abend risk\n"
            "- COBOL-85 vs Enterprise vs MicroFocus dialect-specific syntax\n"
            "- Working-storage data items uninitialized before first reference\n"
            "- 88-level conditions evaluating against group items (semantic surprise)\n"
            "- Recursive PERFORM where one branch never returns\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
