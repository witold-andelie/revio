"""PLC coding rules checker with semantic analysis.

Based on PLCopen Coding Guidelines and Secure PLC Coding Practices (ISA/IEC 62443).
Performs both pattern-based and semantic analysis of IEC 61131-3 Structured Text code.
"""

import re
from collections import defaultdict

from pydantic import BaseModel

from ..parser.plc.st_extractor import STFunctionBlock, STVariable, StructuredTextExtractor


class PLCRuleViolation(BaseModel):
    """A violation of a PLC coding rule."""
    rule_id: str
    rule_name: str
    severity: str  # critical, error, warning, info
    description: str
    line_number: int | None = None
    suggestion: str | None = None


class PLCRulesChecker:
    """Check Structured Text code against PLC coding rules.

    Performs three levels of analysis:
    1. Pattern-based: regex matching for common anti-patterns
    2. Structural: variable scope, function block usage, code organization
    3. Semantic: interlock chains, safety logic, race conditions
    """

    # ============================================================
    # Level 1: Pattern-based rules (regex)
    # ============================================================

    PATTERN_RULES = [
        {
            "id": "PLC-001",
            "name": "Direct I/O address in program body",
            "pattern": re.compile(r"(?<!\w)(%?[IQM]\d+\.\d+|%(?:I|Q|M|DB)\d+)\b", re.IGNORECASE),
            "severity": "warning",
            "description": (
                "Direct I/O addresses (e.g. %I0.0, Q0.1) in program body reduce portability. "
                "Map I/O to symbolic variables in a configuration POU."
            ),
            "suggestion": "Define symbolic names: VAR_INPUT i_StartButton AT %I0.0 : BOOL; END_VAR",
        },
        {
            "id": "PLC-003",
            "name": "Hardcoded magic number",
            "pattern": re.compile(r"(?<![A-Za-z0-9_.])(?:0x[0-9A-Fa-f]{4,}|\d{5,})(?![A-Za-z0-9_.])"),
            "severity": "warning",
            "description": "Large numeric literals make code hard to maintain and audit.",
            "suggestion": "Define named constants: VAR CONSTANT MAX_SPEED : INT := 1000; END_VAR",
        },
        {
            "id": "PLC-005",
            "name": "Unsafe type conversion",
            "pattern": re.compile(
                r"\b(?:INT_TO_|REAL_TO_|DWORD_TO_|WORD_TO_|DINT_TO_|SINT_TO_|UINT_TO_|UDINT_TO_)"
                r"(?:REAL|INT|DINT|SINT|WORD|DWORD|BOOL|BYTE)\b",
                re.IGNORECASE
            ),
            "severity": "info",
            "description": "Explicit type conversions may cause data loss (overflow, truncation).",
            "suggestion": "Add range checks before type conversions: IF value <= MAX_INT THEN ...",
        },
        {
            # M4 fix: v1's regex `/\s*[^/\*]` matched ANY '/' not followed by
            # '/' or '*' — caught file paths in strings, URL examples in
            # comments, and almost any arithmetic. Tightened to require the
            # division operator be flanked by ST identifiers / digits and
            # surrounded by operator-like whitespace, dramatically cutting
            # false positives.
            "id": "PLC-006",
            "name": "Division without zero check",
            "pattern": re.compile(
                r"(?<![/'\"\w])\b\w+\s*/\s*\w+\b(?!['\"])",
                re.IGNORECASE,
            ),
            "severity": "error",
            "description": "Division operations must check for zero divisor to prevent runtime fault.",
            "suggestion": "Add: IF divisor <> 0 THEN result := dividend / divisor; END_IF;",
        },
        {
            "id": "PLC-010",
            "name": "Unsafe string operation",
            "pattern": re.compile(r"\b(?:CONCAT|INSERT|DELETE|REPLACE|LEFT|RIGHT|MID|FIND)\s*\(", re.IGNORECASE),
            "severity": "warning",
            "description": "String operations can cause buffer overflow if result exceeds target length.",
            "suggestion": "Check LEN(result) <= SIZEOF(target) before string operations.",
        },
        {
            "id": "PLC-011",
            "name": "Floating point equality comparison",
            "pattern": re.compile(r"(?:REAL|LREAL)\s*[^<>!]*=\s*[^=]"),
            "severity": "warning",
            "description": "Direct equality comparison on floating-point values is unreliable due to precision errors.",
            "suggestion": "Use range comparison: ABS(a - b) < EPSILON",
        },
        {
            "id": "PLC-012",
            "name": "Pointer dereference without nil check",
            "pattern": "pointer_deref",
            "severity": "error",
            "description": "Pointer dereference without null check can cause undefined behavior.",
        },
        {
            "id": "PLC-020",
            "name": "GOTO statement usage",
            "pattern": re.compile(r"\bGOTO\s+\w+", re.IGNORECASE),
            "severity": "warning",
            "description": "GOTO statements create spaghetti code and are discouraged by PLCopen.",
            "suggestion": "Refactor to use IF/THEN/ELSE or CASE statements instead of GOTO.",
        },
        {
            "id": "PLC-021",
            "name": "Empty control block branch",
            "pattern": re.compile(r"(?:ELSE|CASE\s+\w+:)\s*;", re.IGNORECASE),
            "severity": "info",
            "description": "Empty ELSE or CASE branches may indicate incomplete logic.",
            "suggestion": "Add a comment explaining why the branch is intentionally empty, or add logic.",
        },
        {
            "id": "PLC-022",
            "name": "EXIT statement in loop",
            "pattern": re.compile(r"\bEXIT\b", re.IGNORECASE),
            "severity": "info",
            "description": "EXIT statements can make loop behavior hard to predict.",
            "suggestion": "Consider restructuring the loop condition to avoid EXIT.",
        },
        {
            "id": "PLC-025",
            "name": "RETURN in PROGRAM block",
            "pattern": re.compile(r"\bRETURN\b", re.IGNORECASE),
            "severity": "warning",
            "check_type": "return_in_program",
            "description": "RETURN in a PROGRAM block exits the entire program cycle, which may cause unexpected behavior.",
        },
    ]

    # ============================================================
    # Level 2: Structural rules (require parsed ST)
    # ============================================================

    @classmethod
    def check_code(cls, source_code: str) -> list[PLCRuleViolation]:
        """Run all rule checks on ST source code."""
        violations = []

        # Level 1: Pattern-based checks
        violations.extend(cls._check_patterns(source_code))

        # Level 2: Structural analysis
        try:
            blocks = StructuredTextExtractor.extract_blocks(source_code)
            variables = StructuredTextExtractor.extract_variables(source_code)
            violations.extend(cls._check_structure(source_code, blocks, variables))
            violations.extend(cls._check_semantics(source_code, blocks, variables))
        except Exception:
            # If parsing fails, still return pattern-based results
            pass

        # Deduplicate
        seen = set()
        unique = []
        for v in violations:
            key = (v.rule_id, v.line_number, v.description)
            if key not in seen:
                seen.add(key)
                unique.append(v)

        return unique

    @classmethod
    def _check_patterns(cls, source_code: str) -> list[PLCRuleViolation]:
        """Level 1: Pattern-based regex checks."""
        violations = []
        lines = source_code.split("\n")

        for rule in cls.PATTERN_RULES:
            if rule["pattern"] == "pointer_deref":
                # Special handling for pointer checks
                violations.extend(cls._check_pointers(lines))
                continue

            if rule.get("check_type") == "return_in_program":
                # Only flag RETURN inside PROGRAM blocks
                violations.extend(cls._check_return_in_program(source_code, rule))
                continue

            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("(*"):
                    continue

                if rule["pattern"].search(line):
                    violations.append(PLCRuleViolation(
                        rule_id=rule["id"],
                        rule_name=rule["name"],
                        severity=rule["severity"],
                        description=rule["description"],
                        line_number=i + 1,
                        suggestion=rule.get("suggestion"),
                    ))

        return violations

    @classmethod
    def _check_return_in_program(
        cls, source_code: str, rule: dict
    ) -> list[PLCRuleViolation]:
        """Check for RETURN statements inside PROGRAM blocks."""
        violations = []
        lines = source_code.split("\n")
        in_program = False
        brace_depth = 0

        for i, line in enumerate(lines):
            stripped = line.strip().upper()
            if re.match(r"PROGRAM\s+\w+", stripped):
                in_program = True
            elif stripped.startswith("END_PROGRAM"):
                in_program = False
            elif in_program and re.match(r"\bRETURN\b", stripped):
                violations.append(PLCRuleViolation(
                    rule_id=rule["id"],
                    rule_name=rule["name"],
                    severity=rule["severity"],
                    description=rule["description"],
                    line_number=i + 1,
                    suggestion="Remove RETURN or restructure the program logic.",
                ))
        return violations

    @classmethod
    def _check_pointers(cls, lines: list[str]) -> list[PLCRuleViolation]:
        """Check for unsafe pointer dereferences."""
        violations = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("(*"):
                continue
            # Check for ^ dereference without prior nil check
            if "^" in line and ":=" in line:
                context = "\n".join(lines[max(0, i - 5):i + 1])
                if not re.search(r"<>?\s*NIL|<>?\s*0|IS_NOT_NULL", context, re.IGNORECASE):
                    violations.append(PLCRuleViolation(
                        rule_id="PLC-012",
                        rule_name="Pointer dereference without nil check",
                        severity="error",
                        description="Pointer dereference without null check can cause runtime fault.",
                        line_number=i + 1,
                        suggestion="Add: IF ptr <> NIL THEN value := ptr^; END_IF;",
                    ))
        return violations

    # ============================================================
    # Level 2: Structural analysis
    # ============================================================

    @classmethod
    def _check_structure(
        cls,
        source_code: str,
        blocks: list[STFunctionBlock],
        variables: list[STVariable],
    ) -> list[PLCRuleViolation]:
        """Structural checks on parsed ST code."""
        violations = []

        # PLC-002: Missing watchdog in cyclic code
        violations.extend(cls._check_watchdog(source_code, blocks))

        # PLC-004: Array bounds checking
        violations.extend(cls._check_array_bounds(source_code))

        # PLC-013: Variable naming conventions
        violations.extend(cls._check_naming_conventions(variables))

        # PLC-014: Uninitialized variables
        violations.extend(cls._check_uninitialized(variables))

        # PLC-015: Block size limits
        violations.extend(cls._check_block_size(blocks))

        # PLC-016: Missing comments on safety-critical I/O
        violations.extend(cls._check_io_comments(variables))

        # PLC-023: Deeply nested IF statements
        violations.extend(cls._check_nesting_depth(source_code))

        # PLC-024: Unused variables
        violations.extend(cls._check_unused_variables(source_code, variables))

        return violations

    @classmethod
    def _check_watchdog(
        cls, source_code: str, blocks: list[STFunctionBlock]
    ) -> list[PLCRuleViolation]:
        """Check for watchdog timers in cyclic programs."""
        violations = []

        # Only flag if this looks like a cyclic program (PROGRAM or OB)
        has_cyclic = any(
            b.block_type.upper() in ("PROGRAM", "OB")
            for b in blocks
        )

        if not has_cyclic and not blocks:
            # If no blocks parsed, check for PROGRAM keyword
            if not re.search(r"\bPROGRAM\b", source_code, re.IGNORECASE):
                return violations

        # Check if any timer is used
        has_timer = bool(re.search(
            r"\b(?:TON|TOF|TP|TON_X|TOF_X|TP_X)\s*\(", source_code, re.IGNORECASE
        ))

        # Check if there's a cycle time check
        has_cycle_check = bool(re.search(
            r"\b(?:CYCLE_TIME|WATCHDOG|OB_CYCLE|CycleTime)\b", source_code, re.IGNORECASE
        ))

        if not has_timer and not has_cycle_check:
            violations.append(PLCRuleViolation(
                rule_id="PLC-002",
                rule_name="Missing watchdog timer",
                severity="warning",
                description=(
                    "No watchdog timer or cycle time monitoring detected. "
                    "Safety-critical cyclic programs should monitor execution time."
                ),
                suggestion="Add a TON watchdog: timer(IN := TRUE, PT := T#100MS); IF NOT timer.Q THEN ALARM(); END_IF;",
            ))

        return violations

    @classmethod
    def _check_array_bounds(cls, source_code: str) -> list[PLCRuleViolation]:
        """Check for array access without bounds checking."""
        violations = []
        lines = source_code.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("(*"):
                continue

            # Find array access patterns: array[index]
            array_access = re.findall(r"(\w+)\[(\w+)\]", line)
            for array_name, index_var in array_access:
                # Skip if index is a constant or literal
                if index_var.isdigit() or index_var.isupper():
                    continue

                # Check if bounds check exists in surrounding context
                context = "\n".join(lines[max(0, i - 5):i + 6])
                has_bounds = re.search(
                    rf"(?:UPPER_BOUND|LOWER_BOUND|LEN|SIZEOF)\s*\(\s*{re.escape(array_name)}\s*\)",
                    context, re.IGNORECASE
                )
                has_range_check = re.search(
                    rf"{re.escape(index_var)}\s*(?:<=|<|>=|>)\s*\d+",
                    context, re.IGNORECASE
                )

                if not has_bounds and not has_range_check:
                    violations.append(PLCRuleViolation(
                        rule_id="PLC-004",
                        rule_name="Array access without bounds check",
                        severity="warning",
                        description=f"Array '{array_name}[{index_var}]' accessed without bounds validation.",
                        line_number=i + 1,
                        suggestion=f"Add: IF {index_var} >= 0 AND {index_var} <= UPPER_BOUND({array_name}, 1) THEN ...",
                    ))

        return violations

    @classmethod
    def _check_naming_conventions(
        cls, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check PLCopen naming conventions."""
        violations = []

        for var in variables:
            name = var.name

            # Check input prefix
            if var.scope == "VAR_INPUT" and not name.startswith(("i_", "in_", "I_")):
                violations.append(PLCRuleViolation(
                    rule_id="PLC-013",
                    rule_name="Input variable missing prefix",
                    severity="info",
                    description=(
                        f"Input variable '{name}' should use 'i_' prefix per PLCopen convention."
                    ),
                    suggestion=f"Rename to: i_{name[0].lower()}{name[1:]}",
                ))

            # Check output prefix
            if var.scope == "VAR_OUTPUT" and not name.startswith(("o_", "out_", "O_")):
                violations.append(PLCRuleViolation(
                    rule_id="PLC-013",
                    rule_name="Output variable missing prefix",
                    severity="info",
                    description=(
                        f"Output variable '{name}' should use 'o_' prefix per PLCopen convention."
                    ),
                    suggestion=f"Rename to: o_{name[0].lower()}{name[1:]}",
                ))

            # Check internal variable prefix
            if var.scope == "VAR" and name.startswith(("i_", "o_", "g_")):
                violations.append(PLCRuleViolation(
                    rule_id="PLC-013",
                    rule_name="Internal variable has I/O prefix",
                    severity="info",
                    description=f"Internal variable '{name}' should not use I/O prefixes.",
                    suggestion="Use 'int_' prefix or no prefix for internal variables.",
                ))

        return violations

    @classmethod
    def _check_uninitialized(
        cls, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check for variables without initial values."""
        violations = []

        critical_types = {"REAL", "LREAL", "INT", "DINT", "SINT", "UINT", "UDINT"}

        for var in variables:
            if var.initial_value is None and var.datatype.upper() in critical_types:
                violations.append(PLCRuleViolation(
                    rule_id="PLC-014",
                    rule_name="Variable without initial value",
                    severity="info",
                    description=(
                        f"Variable '{var.name}' ({var.datatype}) has no initial value. "
                        f"On cold start, the value is undefined."
                    ),
                    suggestion=f"Add initial value: {var.name} : {var.datatype} := 0;",
                ))

        return violations

    @classmethod
    def _check_block_size(
        cls, blocks: list[STFunctionBlock]
    ) -> list[PLCRuleViolation]:
        """Check function block size limits."""
        violations = []

        for block in blocks:
            line_count = block.line_end - block.line_start
            if line_count > 200:
                violations.append(PLCRuleViolation(
                    rule_id="PLC-015",
                    rule_name="Function block exceeds 200 lines",
                    severity="warning",
                    description=(
                        f"{block.block_type} '{block.name}' is {line_count} lines. "
                        f"Large blocks are hard to test and maintain."
                    ),
                    suggestion="Split into smaller function blocks with single responsibility.",
                ))

        return violations

    @classmethod
    def _check_io_comments(
        cls, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check that I/O variables have comments."""
        violations = []

        for var in variables:
            if var.scope in ("VAR_INPUT", "VAR_OUTPUT") and var.comment is None:
                violations.append(PLCRuleViolation(
                    rule_id="PLC-016",
                    rule_name="I/O variable missing comment",
                    severity="info",
                    description=(
                        f"{var.scope.replace('VAR_', '')} variable '{var.name}' has no comment. "
                        f"I/O variables should be documented."
                    ),
                    suggestion=f"Add comment: {var.name} : {var.datatype}; // Description",
                ))

        return violations

    @classmethod
    def _check_nesting_depth(cls, source_code: str) -> list[PLCRuleViolation]:
        """Check for deeply nested control structures (PLCopen recommends max 4 levels)."""
        violations = []
        lines = source_code.split("\n")
        depth = 0
        max_depth = 0
        max_depth_line = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("(*"):
                continue

            upper = stripped.upper()

            # Count nesting depth
            if re.match(r"(?:IF|FOR|WHILE|REPEAT|CASE)\b", upper):
                depth += 1
                if depth > max_depth:
                    max_depth = depth
                    max_depth_line = i + 1

            # Detect end of blocks
            if upper.startswith("END_IF") or upper.startswith("END_FOR") or \
               upper.startswith("END_WHILE") or upper.startswith("END_REPEAT") or \
               upper.startswith("END_CASE"):
                depth = max(0, depth - 1)

        if max_depth > 4:
            violations.append(PLCRuleViolation(
                rule_id="PLC-023",
                rule_name="Nesting depth exceeds 4 levels",
                severity="warning",
                description=(
                    f"Code nesting reaches {max_depth} levels (line {max_depth_line}). "
                    f"PLCopen recommends maximum 4 levels for readability."
                ),
                line_number=max_depth_line,
                suggestion="Refactor deeply nested code into separate function blocks.",
            ))

        return violations

    @classmethod
    def _check_unused_variables(
        cls, source_code: str, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check for declared but unused variables."""
        violations = []

        for var in variables:
            # Skip I/O variables (they're used externally)
            if var.scope in ("VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_EXTERNAL"):
                continue

            # Count occurrences of the variable name in the code body
            # (excluding the declaration line)
            pattern = re.compile(rf"\b{re.escape(var.name)}\b")
            occurrences = len(pattern.findall(source_code))

            # Variable appears once = only in declaration = unused
            if occurrences <= 1:
                violations.append(PLCRuleViolation(
                    rule_id="PLC-024",
                    rule_name="Unused variable",
                    severity="info",
                    description=(
                        f"Variable '{var.name}' is declared but never used in the code."
                    ),
                    suggestion=f"Remove unused variable '{var.name}' or add usage.",
                ))

        return violations

    # ============================================================
    # Level 3: Semantic analysis
    # ============================================================

    @classmethod
    def _check_semantics(
        cls,
        source_code: str,
        blocks: list[STFunctionBlock],
        variables: list[STVariable],
    ) -> list[PLCRuleViolation]:
        """Semantic analysis of ST code logic."""
        violations = []

        # PLC-007: Interlock validation
        violations.extend(cls._check_interlocks(source_code, variables))

        # PLC-008: Race condition in output assignments
        violations.extend(cls._check_race_conditions(source_code))

        # PLC-009: Emergency stop pattern
        violations.extend(cls._check_emergency_stop(source_code, variables))

        # PLC-017: Sensor input validation
        violations.extend(cls._check_sensor_validation(source_code, variables))

        # PLC-018: Communication timeout handling
        violations.extend(cls._check_comm_timeout(source_code))

        # PLC-019: Fail-safe defaults
        violations.extend(cls._check_fail_safe(source_code, variables))

        return violations

    @classmethod
    def _check_interlocks(
        cls, source_code: str, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check that SET/RESET operations have proper interlock logic."""
        violations = []
        lines = source_code.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("(*"):
                continue

            # Check for SET/RESET coils or direct output writes
            is_output_write = bool(re.search(
                r"(?:o_|out_|O_)\w+\s*:=\s*TRUE|SET\s*\(", stripped, re.IGNORECASE
            ))
            is_reset = bool(re.search(
                r"(?:o_|out_|O_)\w+\s*:=\s*FALSE|RESET\s*\(", stripped, re.IGNORECASE
            ))

            if is_output_write:
                # Check surrounding context for interlock conditions
                context_start = max(0, i - 10)
                context = "\n".join(lines[context_start:i + 1])

                # Look for safety conditions
                has_interlock = bool(re.search(
                    r"\b(?:AND|IF)\b.*(?:NOT|EMERGENCY|ESTOP|E_STOP|SAFETY|FAULT|ALARM|LIMIT|OVERLOAD)",
                    context, re.IGNORECASE
                ))
                has_stop_condition = bool(re.search(
                    r"NOT\s+(?:o_|out_)?(?:Stop|STOP|Emergency|EMERGENCY|Fault|FAULT)",
                    context, re.IGNORECASE
                ))

                if not has_interlock and not has_stop_condition:
                    violations.append(PLCRuleViolation(
                        rule_id="PLC-007",
                        rule_name="Output write without interlock",
                        severity="warning",
                        description=(
                            f"Output assignment on line {i + 1} lacks visible interlock logic. "
                            f"Physical outputs should be conditional on safety states."
                        ),
                        suggestion="Add interlock: IF NOT Emergency AND NOT Fault THEN o_Motor := TRUE; END_IF;",
                    ))

        return violations

    @classmethod
    def _check_race_conditions(cls, source_code: str) -> list[PLCRuleViolation]:
        """Detect multiple writes to the same output in one cycle."""
        violations = []
        lines = source_code.split("\n")

        # Track output assignments
        output_writes = defaultdict(list)  # variable -> [(line_num, line)]

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("(*"):
                continue

            # Find output assignments
            match = re.match(r"(\w+)\s*:=\s*(.+?);", stripped)
            if match:
                var_name = match.group(1)
                # Check if it's an output variable
                if var_name.startswith(("o_", "out_", "O_")) or var_name.isupper():
                    output_writes[var_name].append((i + 1, stripped))

        # Flag variables with multiple unconditional writes
        for var_name, writes in output_writes.items():
            if len(writes) > 1:
                # Check if writes are in IF/ELSE branches (conditional)
                lines_nums = [w[0] for w in writes]
                # Simple heuristic: if writes are close together without IF/ELSE, flag it
                for j in range(len(writes) - 1):
                    line_a = writes[j][0]
                    line_b = writes[j + 1][0]
                    if line_b - line_a < 3:
                        violations.append(PLCRuleViolation(
                            rule_id="PLC-008",
                            rule_name="Potential race condition in output assignment",
                            severity="error",
                            description=(
                                f"Output '{var_name}' is written on lines {line_a} and {line_b}. "
                                f"Multiple writes in the same cycle create a race condition — "
                                f"only the last write takes effect."
                            ),
                            line_number=line_a,
                            suggestion="Consolidate writes into a single assignment with proper conditions.",
                        ))

        return violations

    @classmethod
    def _check_emergency_stop(
        cls, source_code: str, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check for emergency stop logic in programs with physical outputs."""
        violations = []

        # Check if there are physical outputs
        has_outputs = any(
            v.scope == "VAR_OUTPUT" and v.datatype.upper() == "BOOL"
            for v in variables
        )

        if not has_outputs:
            return violations

        # Check for emergency stop handling
        has_estop = bool(re.search(
            r"\b(?:Emergency|E_Stop|EStop|EMERGENCY|E_STOP|NOTRHALT|NotAus)\b",
            source_code, re.IGNORECASE
        ))

        has_safety_off = bool(re.search(
            r"\b(?:SafeTorqueOff|STO|SafetyOff|SAFE_STATE)\b",
            source_code, re.IGNORECASE
        ))

        if not has_estop and not has_safety_off:
            violations.append(PLCRuleViolation(
                rule_id="PLC-009",
                rule_name="Missing emergency stop handling",
                severity="error",
                description=(
                    "Block has physical BOOL outputs but no emergency stop logic detected. "
                    "All programs controlling actuators must implement E-stop handling."
                ),
                suggestion="Add: IF NOT i_EmergencyStop THEN o_Motor := FALSE; o_Valve := FALSE; END_IF;",
            ))

        return violations

    @classmethod
    def _check_sensor_validation(
        cls, source_code: str, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check that sensor inputs are validated before use."""
        violations = []

        # Find sensor-type input variables
        sensor_types = {"REAL", "LREAL", "INT", "DINT"}
        sensor_inputs = [
            v for v in variables
            if v.scope == "VAR_INPUT" and v.datatype.upper() in sensor_types
        ]

        for sensor in sensor_inputs:
            # Check if the sensor value is used in comparisons or calculations
            # without range checking
            if re.search(rf"\b{re.escape(sensor.name)}\b", source_code):
                has_range_check = bool(re.search(
                    rf"{re.escape(sensor.name)}\s*(?:<=?|>=?|<>|>|<)\s*\d",
                    source_code
                ))
                has_limit_check = bool(re.search(
                    rf"(?:LIMIT|CLAMP|MIN|MAX)\s*\(",
                    source_code, re.IGNORECASE
                ))

                if not has_range_check and not has_limit_check:
                    violations.append(PLCRuleViolation(
                        rule_id="PLC-017",
                        rule_name="Sensor input without range validation",
                        severity="warning",
                        description=(
                            f"Sensor input '{sensor.name}' ({sensor.datatype}) is used "
                            f"without validating against physical limits."
                        ),
                        suggestion=f"Add: IF {sensor.name} >= MIN_VAL AND {sensor.name} <= MAX_VAL THEN ...",
                    ))

        return violations

    @classmethod
    def _check_comm_timeout(cls, source_code: str) -> list[PLCRuleViolation]:
        """Check for communication timeout handling."""
        violations = []

        # Check for communication patterns
        has_comm = bool(re.search(
            r"\b(?:SEND|RECEIVE|MODBUS|PROFINET|ETHERNET|SOCKET|TCP|UDP|MB_CLIENT|MB_SERVER)\b",
            source_code, re.IGNORECASE
        ))

        if has_comm:
            has_timeout = bool(re.search(
                r"\b(?:TIMEOUT|Timeout|timeout|T#\d|TIME#)\b",
                source_code, re.IGNORECASE
            ))
            has_heartbeat = bool(re.search(
                r"\b(?:HEARTBEAT|Heartbeat|heartbeat|ALIVE|WatchDog)\b",
                source_code, re.IGNORECASE
            ))

            if not has_timeout and not has_heartbeat:
                violations.append(PLCRuleViolation(
                    rule_id="PLC-018",
                    rule_name="Communication without timeout handling",
                    severity="error",
                    description=(
                        "Communication operations detected without timeout or heartbeat monitoring. "
                        "Network failures can cause indefinite blocking."
                    ),
                    suggestion="Add timeout: timer(IN := comm_active, PT := T#5S); IF timer.Q THEN comm_error := TRUE; END_IF;",
                ))

        return violations

    @classmethod
    def _check_fail_safe(
        cls, source_code: str, variables: list[STVariable]
    ) -> list[PLCRuleViolation]:
        """Check for fail-safe default behavior."""
        violations = []

        outputs = [v for v in variables if v.scope == "VAR_OUTPUT"]

        for out in outputs:
            # Check if output has a safe default (FALSE/0) on initialization
            if out.initial_value is None and out.datatype.upper() == "BOOL":
                violations.append(PLCRuleViolation(
                    rule_id="PLC-019",
                    rule_name="Output without fail-safe default",
                    severity="warning",
                    description=(
                        f"Output '{out.name}' has no initial value. "
                        f"On power-up, outputs should default to safe state (FALSE/0)."
                    ),
                    suggestion=f"Set initial value: {out.name} : BOOL := FALSE;",
                ))

        return violations
