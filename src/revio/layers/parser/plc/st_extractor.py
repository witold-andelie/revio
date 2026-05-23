"""Extract and parse IEC 61131-3 Structured Text code.

Ported from v1's src/plc/st_extractor.py with fix: field renamed
`data_type` → `datatype` to match plc_rules.py's usage (v1 had an
inconsistency that caused AttributeError at runtime).
"""

from __future__ import annotations

import re

from pydantic import BaseModel


class STVariable(BaseModel):
    """A Structured Text variable declaration."""

    name: str
    datatype: str                # Was `data_type` in v1 — renamed for consistency
    scope: str                   # VAR | VAR_INPUT | VAR_OUTPUT | VAR_IN_OUT | VAR_GLOBAL | VAR_TEMP
    initial_value: str | None = None
    comment: str | None = None


class STFunctionBlock(BaseModel):
    """A Structured Text function block, function, or program."""

    name: str
    block_type: str              # FUNCTION_BLOCK | FUNCTION | PROGRAM
    return_type: str | None = None
    variables: list[STVariable] = []
    body: str = ""
    line_start: int = 0
    line_end: int = 0


class StructuredTextExtractor:
    """Extract function blocks + variable declarations from IEC 61131-3 ST."""

    # --- Regex patterns ------------------------------------------------------

    VAR_BLOCK_PATTERN = re.compile(
        r"(VAR(?:_INPUT|_OUTPUT|_IN_OUT|_GLOBAL|_TEMP)?)\s*(?::\s*(.+?))?\s*\n(.*?)END_VAR",
        re.DOTALL | re.IGNORECASE,
    )

    VAR_DECL_PATTERN = re.compile(
        r"(\w+)\s*:\s*(\w+(?:\s*\([^)]*\))?)(?:\s*:=\s*(.+?))?;",
        re.IGNORECASE,
    )

    FUNCTION_BLOCK_PATTERN = re.compile(
        r"(FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+(\w+)(?:\s*:\s*(\w+))?",
        re.IGNORECASE,
    )

    # --- Block extraction ----------------------------------------------------

    @classmethod
    def extract_blocks(cls, source_code: str) -> list[STFunctionBlock]:
        """Extract function blocks / functions / programs from ST source."""
        blocks: list[STFunctionBlock] = []
        lines = source_code.split("\n")
        current_block: STFunctionBlock | None = None

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Block start
            match = cls.FUNCTION_BLOCK_PATTERN.match(stripped)
            if match:
                # Close any previously-open block (rare in well-formed code,
                # but defensive)
                if current_block is not None:
                    current_block.line_end = i - 1
                    blocks.append(current_block)

                block_type = match.group(1)
                name = match.group(2)
                return_type = match.group(3)

                current_block = STFunctionBlock(
                    name=name,
                    block_type=block_type,
                    return_type=return_type,
                    line_start=i,
                )
                continue

            # Block end
            if current_block is not None and stripped.upper().startswith("END_"):
                expected_end = f"END_{current_block.block_type.upper()}"
                if stripped.upper().startswith(expected_end):
                    current_block.line_end = i
                    blocks.append(current_block)
                    current_block = None
                    continue

            # Accumulate body
            if current_block is not None:
                current_block.body += line + "\n"

        # Close trailing block
        if current_block is not None:
            current_block.line_end = len(lines) - 1
            blocks.append(current_block)

        return blocks

    # --- Variable extraction --------------------------------------------------

    @classmethod
    def extract_variables(cls, source_code: str) -> list[STVariable]:
        """Extract variable declarations from VAR / VAR_INPUT / ... blocks."""
        variables: list[STVariable] = []

        for var_block_match in cls.VAR_BLOCK_PATTERN.finditer(source_code):
            scope = var_block_match.group(1).upper()
            block_content = var_block_match.group(3)

            for var_match in cls.VAR_DECL_PATTERN.finditer(block_content):
                name = var_match.group(1)
                datatype = var_match.group(2)
                initial_value = var_match.group(3)
                variables.append(STVariable(
                    name=name,
                    datatype=datatype.strip(),
                    scope=scope,
                    initial_value=initial_value.strip() if initial_value else None,
                ))

        return variables

    # --- Convenience helpers --------------------------------------------------

    @classmethod
    def extract_changed_region(
        cls, source_code: str, start_line: int, end_line: int
    ) -> str | None:
        """Return the body of the enclosing block for a changed line range."""
        for block in cls.extract_blocks(source_code):
            if block.line_start <= start_line <= block.line_end:
                return block.body
        return None
