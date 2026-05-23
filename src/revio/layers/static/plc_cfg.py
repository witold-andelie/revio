"""Control Flow Graph (CFG) analyzer for IEC 61131-3 Structured Text.

Builds a control flow graph from ST code, enabling:
  - Basic block identification
  - Loop detection (natural loops, back edges)
  - Unreachable code detection
  - Data flow analysis (def-use chains)
  - Variable liveness analysis

This provides the foundation for deep semantic analysis beyond
pattern matching and structural checks.

Basic Block: A maximal sequence of instructions with:
  - Single entry point (first instruction)
  - Single exit point (last instruction)
  - No branches except at the end
  - No branch targets except at the beginning

CFG Edge Types:
  - Fall-through: sequential execution to next block
  - Conditional branch: IF condition THEN target
  - Unconditional branch: GOTO target
  - Loop back: edge from loop body to loop header
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class EdgeType(str, Enum):
    """Types of edges in the CFG."""
    FALL_THROUGH = "fall_through"
    TRUE_BRANCH = "true_branch"
    FALSE_BRANCH = "false_branch"
    UNCONDITIONAL = "unconditional"
    LOOP_BACK = "loop_back"
    FUNCTION_CALL = "function_call"


class BlockType(str, Enum):
    """Types of basic blocks."""
    NORMAL = "normal"
    CONDITIONAL = "conditional"  # Ends with IF/CASE
    LOOP_HEADER = "loop_header"  # Target of a back edge
    LOOP_BODY = "loop_body"
    ENTRY = "entry"
    EXIT = "exit"  # Contains RETURN
    UNREACHABLE = "unreachable"


@dataclass
class Statement:
    """A single ST statement."""
    text: str
    line_number: int
    defines: set[str] = field(default_factory=set)   # Variables written
    uses: set[str] = field(default_factory=set)       # Variables read
    is_control_flow: bool = False  # IF, CASE, FOR, WHILE, REPEAT, GOTO, RETURN, EXIT


@dataclass
class BasicBlock:
    """A basic block in the control flow graph."""
    block_id: int
    statements: list[Statement] = field(default_factory=list)
    block_type: BlockType = BlockType.NORMAL
    label: str = ""  # For GOTO targets

    @property
    def first_line(self) -> int:
        return self.statements[0].line_number if self.statements else 0

    @property
    def last_line(self) -> int:
        return self.statements[-1].line_number if self.statements else 0

    @property
    def defines(self) -> set[str]:
        """All variables defined in this block."""
        return set().union(*(s.defines for s in self.statements))

    @property
    def uses(self) -> set[str]:
        """All variables used in this block."""
        return set().union(*(s.uses for s in self.statements))

    @property
    def is_empty(self) -> bool:
        return len(self.statements) == 0


@dataclass
class CFGEdge:
    """An edge in the control flow graph."""
    source_id: int
    target_id: int
    edge_type: EdgeType = EdgeType.FALL_THROUGH
    condition: str = ""  # For conditional branches


@dataclass
class Loop:
    """A detected loop in the CFG."""
    header_id: int
    body_ids: set[int]
    back_edge_source: int
    is_natural: bool = True


@dataclass
class DefUseEntry:
    """A definition-use pair for a variable."""
    variable: str
    def_block: int
    def_line: int
    use_block: int
    use_line: int


@dataclass
class CFG:
    """Complete control flow graph."""
    blocks: dict[int, BasicBlock] = field(default_factory=dict)
    edges: list[CFGEdge] = field(default_factory=list)
    entry_block_id: int = 0
    exit_block_ids: set[int] = field(default_factory=set)
    loops: list[Loop] = field(default_factory=list)
    unreachable_block_ids: set[int] = field(default_factory=set)
    def_use_chains: list[DefUseEntry] = field(default_factory=list)

    @property
    def successors(self) -> dict[int, list[int]]:
        """Map from block_id to list of successor block_ids."""
        succ = defaultdict(list)
        for edge in self.edges:
            succ[edge.source_id].append(edge.target_id)
        return dict(succ)

    @property
    def predecessors(self) -> dict[int, list[int]]:
        """Map from block_id to list of predecessor block_ids."""
        pred = defaultdict(list)
        for edge in self.edges:
            pred[edge.target_id].append(edge.source_id)
        return dict(pred)


@dataclass
class CFGAnalysisResult:
    """Results of CFG analysis."""
    cfg: CFG
    unreachable_lines: list[int] = field(default_factory=list)
    loop_lines: list[tuple[int, int]] = field(default_factory=list)  # (start, end) of loops
    warnings: list[str] = field(default_factory=list)
    variable_def_use: dict[str, list[DefUseEntry]] = field(default_factory=dict)


class CFGBuilder:
    """Build a Control Flow Graph from ST source code."""

    # Control flow keywords
    CF_KEYWORDS = {"IF", "CASE", "FOR", "WHILE", "REPEAT", "GOTO", "RETURN", "EXIT"}

    @classmethod
    def build_cfg(cls, source_code: str) -> CFGAnalysisResult:
        """Build CFG from ST source code and perform analysis."""
        # Step 1: Parse statements
        statements = cls._parse_statements(source_code)

        # Step 2: Build basic blocks
        blocks = cls._build_basic_blocks(statements)

        # Step 3: Build edges
        edges = cls._build_edges(blocks, source_code)

        # Step 4: Create CFG
        cfg = CFG(
            blocks=blocks,
            edges=edges,
            entry_block_id=min(blocks.keys()) if blocks else 0,
        )

        # Step 5: Detect unreachable blocks
        cfg.unreachable_block_ids = cls._find_unreachable(cfg)

        # Step 6: Detect loops
        cfg.loops = cls._detect_loops(cfg)

        # Step 7: Build def-use chains
        cfg.def_use_chains = cls._build_def_use_chains(cfg)

        # Identify exit blocks
        for block in blocks.values():
            if any(s.text.strip().upper().startswith("RETURN") for s in block.statements):
                cfg.exit_block_ids.add(block.block_id)

        # Build analysis result
        unreachable_lines = []
        for bid in cfg.unreachable_block_ids:
            block = blocks.get(bid)
            if block:
                unreachable_lines.extend(s.line_number for s in block.statements)

        loop_lines = []
        for loop in cfg.loops:
            header = blocks.get(loop.header_id)
            if header:
                # Find the range of lines in the loop
                all_lines = set()
                for bid in loop.body_ids:
                    block = blocks.get(bid)
                    if block:
                        all_lines.update(s.line_number for s in block.statements)
                if all_lines:
                    loop_lines.append((min(all_lines), max(all_lines)))

        # Build per-variable def-use chains
        var_def_use = defaultdict(list)
        for entry in cfg.def_use_chains:
            var_def_use[entry.variable].append(entry)

        warnings = []
        if unreachable_lines:
            warnings.append(f"Found {len(unreachable_lines)} unreachable statement(s)")
        if cfg.loops:
            warnings.append(f"Found {len(cfg.loops)} loop(s)")

        return CFGAnalysisResult(
            cfg=cfg,
            unreachable_lines=unreachable_lines,
            loop_lines=loop_lines,
            warnings=warnings,
            variable_def_use=dict(var_def_use),
        )

    @classmethod
    def _parse_statements(cls, source_code: str) -> list[Statement]:
        """Parse ST source code into individual statements."""
        statements = []
        lines = source_code.split("\n")
        current_stmt = []
        current_line = 0
        depth = 0

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith("//") or stripped.startswith("(*"):
                continue

            # Track nesting depth for multi-line constructs
            upper = stripped.upper()

            if not current_stmt:
                current_line = i + 1

            current_stmt.append(stripped)

            # Check if statement is complete (ends with ; or is a block keyword)
            is_block_start = re.match(
                r"(?:IF|CASE|FOR|WHILE|REPEAT)\b", upper
            )
            is_block_end = re.match(
                r"(?:END_IF|END_CASE|END_FOR|END_WHILE|END_REPEAT|ELSE|ELSIF|UNTIL)\b",
                upper,
            )

            if is_block_start:
                depth += 1
                stmt_text = " ".join(current_stmt)
                stmt = cls._analyze_statement(stmt_text, current_line)
                statements.append(stmt)
                current_stmt = []
            elif is_block_end:
                depth = max(0, depth - 1)
                stmt_text = " ".join(current_stmt)
                stmt = cls._analyze_statement(stmt_text, current_line)
                statements.append(stmt)
                current_stmt = []
            elif stripped.endswith(";") and depth == 0:
                stmt_text = " ".join(current_stmt)
                stmt = cls._analyze_statement(stmt_text, current_line)
                statements.append(stmt)
                current_stmt = []

        # Handle remaining statement
        if current_stmt:
            stmt_text = " ".join(current_stmt)
            stmt = cls._analyze_statement(stmt_text, current_line)
            statements.append(stmt)

        return statements

    @classmethod
    def _analyze_statement(cls, text: str, line_number: int) -> Statement:
        """Analyze a single statement for defines, uses, and control flow."""
        upper = text.strip().upper()
        is_cf = False

        # Detect control flow statements
        if re.match(r"(?:IF|CASE|FOR|WHILE|REPEAT|GOTO|RETURN|EXIT|ELSE|ELSIF|UNTIL)\b", upper):
            is_cf = True

        # Extract variable definitions (left side of :=)
        defines = set()
        assign_match = re.match(r"(\w+)\s*:=", text)
        if assign_match:
            defines.add(assign_match.group(1))

        # Extract variable uses
        uses = set()
        # Find all identifiers (skip keywords and literals)
        keywords = {
            "IF", "THEN", "ELSE", "ELSIF", "END_IF", "CASE", "OF", "END_CASE",
            "FOR", "TO", "BY", "DO", "END_FOR", "WHILE", "END_WHILE",
            "REPEAT", "UNTIL", "END_REPEAT", "GOTO", "RETURN", "EXIT",
            "AND", "OR", "XOR", "NOT", "MOD", "TRUE", "FALSE",
            "INT", "BOOL", "REAL", "TIME", "STRING", "DINT", "SINT", "UINT",
        }

        for match in re.finditer(r"\b([A-Za-z_]\w*)\b", text):
            name = match.group(1)
            if name.upper() not in keywords and not name[0].isdigit():
                uses.add(name)

        # Remove defined variable from uses (it's defined, not used, on the left side)
        uses -= defines

        return Statement(
            text=text,
            line_number=line_number,
            defines=defines,
            uses=uses,
            is_control_flow=is_cf,
        )

    @classmethod
    def _build_basic_blocks(cls, statements: list[Statement]) -> dict[int, BasicBlock]:
        """Build basic blocks from statements."""
        if not statements:
            return {}

        blocks = {}
        block_id = 0
        current_block = BasicBlock(block_id=block_id, block_type=BlockType.ENTRY)
        blocks[block_id] = current_block

        for stmt in statements:
            upper = stmt.text.strip().upper()

            # Start a new block at control flow boundaries
            if stmt.is_control_flow and current_block.statements:
                # Close current block
                block_id += 1
                current_block = BasicBlock(block_id=block_id)
                blocks[block_id] = current_block

            current_block.statements.append(stmt)

            # End block after control flow statements that change flow
            if re.match(r"(?:GOTO|RETURN|EXIT)\b", upper):
                current_block.block_type = BlockType.EXIT if "RETURN" in upper else BlockType.NORMAL
                block_id += 1
                current_block = BasicBlock(block_id=block_id)
                blocks[block_id] = current_block

            # End block after IF/THEN (before ELSE/ELSIF)
            if re.match(r"(?:ELSE|ELSIF)\b", upper):
                current_block.block_type = BlockType.CONDITIONAL
                block_id += 1
                current_block = BasicBlock(block_id=block_id)
                blocks[block_id] = current_block

        # Remove empty blocks
        blocks = {bid: b for bid, b in blocks.items() if not b.is_empty}

        # Re-number blocks
        renumbered = {}
        for new_id, (old_id, block) in enumerate(sorted(blocks.items())):
            block.block_id = new_id
            renumbered[new_id] = block

        return renumbered

    @classmethod
    def _build_edges(cls, blocks: dict[int, BasicBlock], source_code: str) -> list[CFGEdge]:
        """Build edges between basic blocks."""
        edges = []
        block_ids = sorted(blocks.keys())

        for i, bid in enumerate(block_ids):
            block = blocks[bid]
            if not block.statements:
                continue

            last_stmt = block.statements[-1]
            upper = last_stmt.text.strip().upper()

            # RETURN → no outgoing edges (exit block)
            if "RETURN" in upper:
                continue

            # GOTO → unconditional edge to label
            goto_match = re.match(r"GOTO\s+(\w+)\s*;", upper, re.IGNORECASE)
            if goto_match:
                label = goto_match.group(1)
                target_id = cls._find_label_block(blocks, label)
                if target_id is not None:
                    edges.append(CFGEdge(
                        source_id=bid,
                        target_id=target_id,
                        edge_type=EdgeType.UNCONDITIONAL,
                        condition=f"GOTO {label}",
                    ))
                continue

            # IF/THEN → true branch to next block, false branch to ELSE or after END_IF
            if re.match(r"IF\b", upper):
                # True branch: next block
                if i + 1 < len(block_ids):
                    edges.append(CFGEdge(
                        source_id=bid,
                        target_id=block_ids[i + 1],
                        edge_type=EdgeType.TRUE_BRANCH,
                        condition=cls._extract_condition(last_stmt.text),
                    ))
                # False branch: find ELSE/END_IF block
                else_block = cls._find_else_block(blocks, block_ids, i)
                if else_block is not None:
                    edges.append(CFGEdge(
                        source_id=bid,
                        target_id=else_block,
                        edge_type=EdgeType.FALSE_BRANCH,
                    ))
                continue

            # CASE → multiple outgoing edges
            if re.match(r"CASE\b", upper):
                # Simplified: edge to each subsequent block until END_CASE
                for j in range(i + 1, min(i + 10, len(block_ids))):
                    edges.append(CFGEdge(
                        source_id=bid,
                        target_id=block_ids[j],
                        edge_type=EdgeType.CONDITIONAL,
                    ))
                continue

            # FOR/WHILE/REPEAT → loop edges
            if re.match(r"(?:FOR|WHILE)\b", upper):
                # True branch: loop body (next block)
                if i + 1 < len(block_ids):
                    edges.append(CFGEdge(
                        source_id=bid,
                        target_id=block_ids[i + 1],
                        edge_type=EdgeType.TRUE_BRANCH,
                        condition="loop condition",
                    ))
                # False branch: after loop
                after_loop = cls._find_after_loop(blocks, block_ids, i)
                if after_loop is not None:
                    edges.append(CFGEdge(
                        source_id=bid,
                        target_id=after_loop,
                        edge_type=EdgeType.FALSE_BRANCH,
                    ))
                continue

            # Normal block → fall-through to next
            if i + 1 < len(block_ids):
                edges.append(CFGEdge(
                    source_id=bid,
                    target_id=block_ids[i + 1],
                    edge_type=EdgeType.FALL_THROUGH,
                ))

        return edges

    @classmethod
    def _find_label_block(cls, blocks: dict[int, BasicBlock], label: str) -> int | None:
        """Find the block that contains a GOTO label."""
        label_upper = label.upper()
        for bid, block in blocks.items():
            for stmt in block.statements:
                if stmt.text.strip().upper().startswith(f"{label_upper}:"):
                    return bid
        return None

    @classmethod
    def _find_else_block(
        cls, blocks: dict[int, BasicBlock], block_ids: list[int], current_idx: int
    ) -> int | None:
        """Find the ELSE branch block for an IF statement."""
        for j in range(current_idx + 1, len(block_ids)):
            block = blocks[block_ids[j]]
            for stmt in block.statements:
                upper = stmt.text.strip().upper()
                if re.match(r"(?:ELSE|ELSIF|END_IF)\b", upper):
                    return block_ids[j]
        return None

    @classmethod
    def _find_after_loop(
        cls, blocks: dict[int, BasicBlock], block_ids: list[int], current_idx: int
    ) -> int | None:
        """Find the block after a loop (END_FOR/END_WHILE/END_REPEAT)."""
        depth = 1
        for j in range(current_idx + 1, len(block_ids)):
            block = blocks[block_ids[j]]
            for stmt in block.statements:
                upper = stmt.text.strip().upper()
                if re.match(r"(?:FOR|WHILE|REPEAT)\b", upper):
                    depth += 1
                if re.match(r"(?:END_FOR|END_WHILE|END_REPEAT|UNTIL)\b", upper):
                    depth -= 1
                    if depth == 0:
                        if j + 1 < len(block_ids):
                            return block_ids[j + 1]
                        return None
        return None

    @classmethod
    def _extract_condition(cls, text: str) -> str:
        """Extract the condition from an IF statement."""
        match = re.search(r"IF\s+(.+?)\s+THEN", text, re.IGNORECASE)
        if match:
            return match.group(1)
        return ""

    @classmethod
    def _find_unreachable(cls, cfg: CFG) -> set[int]:
        """Find unreachable blocks using BFS from entry."""
        if not cfg.blocks:
            return set()

        reachable = set()
        worklist = [cfg.entry_block_id]
        successors = cfg.successors

        while worklist:
            bid = worklist.pop()
            if bid in reachable:
                continue
            if bid not in cfg.blocks:
                continue
            reachable.add(bid)
            for succ in successors.get(bid, []):
                if succ not in reachable:
                    worklist.append(succ)

        return set(cfg.blocks.keys()) - reachable

    @classmethod
    def _detect_loops(cls, cfg: CFG) -> list[Loop]:
        """Detect natural loops using DFS to find back edges."""
        loops = []
        successors = cfg.successors

        # DFS to find back edges
        visited = set()
        in_stack = set()
        back_edges = []

        def dfs(node: int):
            visited.add(node)
            in_stack.add(node)
            for succ in successors.get(node, []):
                if succ in in_stack:
                    back_edges.append((node, succ))
                elif succ not in visited:
                    dfs(succ)
            in_stack.discard(node)

        if cfg.entry_block_id in cfg.blocks:
            dfs(cfg.entry_block_id)

        # Build natural loops from back edges
        for source, header in back_edges:
            loop_body = {header}
            if source != header:
                loop_body.add(source)
                # Find all nodes that can reach source without going through header
                worklist = [source]
                while worklist:
                    node = worklist.pop()
                    for pred in cfg.predecessors.get(node, []):
                        if pred not in loop_body and pred != header:
                            loop_body.add(pred)
                            worklist.append(pred)

            # Mark header as loop header
            if header in cfg.blocks:
                cfg.blocks[header].block_type = BlockType.LOOP_HEADER

            loops.append(Loop(
                header_id=header,
                body_ids=loop_body,
                back_edge_source=source,
            ))

        return loops

    @classmethod
    def _build_def_use_chains(cls, cfg: CFG) -> list[DefUseEntry]:
        """Build definition-use chains for data flow analysis."""
        chains = []

        # For each block, find definitions and uses
        for bid, block in cfg.blocks.items():
            for stmt in block.statements:
                for var in stmt.uses:
                    # Find the reaching definition for this use
                    def_info = cls._find_reaching_def(cfg, bid, var)
                    if def_info:
                        chains.append(DefUseEntry(
                            variable=var,
                            def_block=def_info[0],
                            def_line=def_info[1],
                            use_block=bid,
                            use_line=stmt.line_number,
                        ))

        return chains

    @classmethod
    def _find_reaching_def(
        cls, cfg: CFG, use_block_id: int, variable: str
    ) -> tuple[int, int] | None:
        """Find the reaching definition for a variable used in a block.

        Uses backward traversal to find the closest definition.
        """
        # Check current block first (backward from use point)
        block = cfg.blocks.get(use_block_id)
        if block:
            for stmt in reversed(block.statements):
                if variable in stmt.defines:
                    return (use_block_id, stmt.line_number)

        # Check predecessor blocks (simplified: only immediate predecessors)
        predecessors = cfg.predecessors.get(use_block_id, [])
        for pred_id in predecessors:
            pred_block = cfg.blocks.get(pred_id)
            if pred_block:
                for stmt in reversed(pred_block.statements):
                    if variable in stmt.defines:
                        return (pred_id, stmt.line_number)

        return None


class CFGAnalyzer:
    """High-level interface for CFG analysis with PLC rule integration."""

    @classmethod
    def analyze(cls, source_code: str) -> list[dict]:
        """Analyze ST source code using CFG and return findings.

        Returns a list of finding dicts with keys:
        - rule_id, rule_name, severity, description, line_number
        """
        findings = []

        try:
            result = CFGBuilder.build_cfg(source_code)
        except Exception as e:
            logger.debug(f"CFG analysis failed: {e}")
            return findings

        cfg = result.cfg

        # Check for unreachable code
        if result.unreachable_lines:
            findings.append({
                "rule_id": "CFG-001",
                "rule_name": "Unreachable code detected",
                "severity": "warning",
                "description": (
                    f"Found {len(result.unreachable_lines)} unreachable statement(s) "
                    f"at line(s): {', '.join(str(l) for l in sorted(result.unreachable_lines)[:5])}"
                ),
                "line_number": min(result.unreachable_lines),
            })

        # Check for loops without exit conditions
        for loop in cfg.loops:
            header = cfg.blocks.get(loop.header_id)
            if header:
                # Check if loop has an EXIT or condition
                has_exit = False
                for bid in loop.body_ids:
                    block = cfg.blocks.get(bid)
                    if block:
                        for stmt in block.statements:
                            if stmt.text.strip().upper().startswith("EXIT"):
                                has_exit = True

                if not has_exit:
                    # Check if FOR/WHILE has a condition
                    first_stmt = header.statements[0] if header.statements else None
                    if first_stmt:
                        upper = first_stmt.text.strip().upper()
                        if "REPEAT" in upper:
                            # REPEAT/UNTIL always has exit condition
                            continue

                    findings.append({
                        "rule_id": "CFG-002",
                        "rule_name": "Loop without visible exit",
                        "severity": "warning",
                        "description": (
                            f"Loop at line {header.first_line} may not have a clear exit condition. "
                            f"Verify that the loop terminates under all conditions."
                        ),
                        "line_number": header.first_line,
                    })

        # Check for variables used before definition
        for entry in cfg.def_use_chains:
            if entry.def_block is None:
                findings.append({
                    "rule_id": "CFG-003",
                    "rule_name": "Variable used before definition",
                    "severity": "warning",
                    "description": (
                        f"Variable '{entry.variable}' is used at line {entry.use_line} "
                        f"but may not be defined before use."
                    ),
                    "line_number": entry.use_line,
                })

        # Check for dead stores (defined but never used)
        all_used_vars = set()
        for entry in cfg.def_use_chains:
            all_used_vars.add(entry.variable)

        for bid, block in cfg.blocks.items():
            for stmt in block.statements:
                for var in stmt.defines:
                    # Check if this definition is ever used
                    is_used = any(
                        e.variable == var and e.def_line == stmt.line_number
                        for e in cfg.def_use_chains
                    )
                    if not is_used and var not in all_used_vars:
                        findings.append({
                            "rule_id": "CFG-004",
                            "rule_name": "Dead store (value never read)",
                            "severity": "info",
                            "description": (
                                f"Variable '{var}' is assigned at line {stmt.line_number} "
                                f"but the value is never read before being overwritten."
                            ),
                            "line_number": stmt.line_number,
                        })

        # Check for high cyclomatic complexity
        complexity = cls._cyclomatic_complexity(cfg)
        if complexity > 15:
            findings.append({
                "rule_id": "CFG-005",
                "rule_name": "High cyclomatic complexity",
                "severity": "warning",
                "description": (
                    f"Cyclomatic complexity is {complexity} (recommended: ≤15). "
                    f"High complexity makes code hard to test and maintain."
                ),
                "line_number": 1,
            })

        return findings

    @classmethod
    def _cyclomatic_complexity(cls, cfg: CFG) -> int:
        """Calculate McCabe's cyclomatic complexity.

        V(G) = E - N + 2P
        E = number of edges
        N = number of nodes (blocks)
        P = number of connected components (usually 1)
        """
        n = len(cfg.blocks)
        e = len(cfg.edges)
        p = 1  # Single connected component
        return e - n + 2 * p

    @classmethod
    def get_cfg_summary(cls, source_code: str) -> dict:
        """Get a summary of the CFG analysis."""
        try:
            result = CFGBuilder.build_cfg(source_code)
            cfg = result.cfg

            return {
                "total_blocks": len(cfg.blocks),
                "total_edges": len(cfg.edges),
                "entry_block": cfg.entry_block_id,
                "exit_blocks": list(cfg.exit_block_ids),
                "unreachable_blocks": list(cfg.unreachable_block_ids),
                "loops": len(cfg.loops),
                "cyclomatic_complexity": cls._cyclomatic_complexity(cfg),
                "def_use_chains": len(cfg.def_use_chains),
                "warnings": result.warnings,
            }
        except Exception as e:
            return {"error": str(e)}
