"""Function Block Diagram (FBD) to Structured Text (ST) converter.

Converts graphical FBD into equivalent ST code by analyzing data flow
through connected function blocks. Uses topological sort on the data
dependency graph to produce correct sequential ST output.

FBD Element Mapping:
  Logic:     AND, OR, XOR, NOT          → boolean operators
  Arithmetic: ADD, SUB, MUL, DIV, MOD   → arithmetic operators
  Comparison: GT, GE, LT, LE, EQ, NE    → comparison operators
  Data move:  MOVE, SHL, SHR, ROL, ROR  → assignments/bit ops
  Timers:    TON, TOF, TP               → FB calls (IEC 61131-3)
  Counters:  CTU, CTD, CTUD             → FB calls (IEC 61131-3)
  Latching:  SR, RS                     → FB calls (IEC 61131-3)
  Edge detect: R_TRIG, F_TRIG           → FB calls (IEC 61131-3)
  Selection:  SEL, MUX, DEMUX           → CASE/IF expressions
  Type conv:  INT_TO_REAL, etc.         → direct function calls

Data flow: Input variables → Block inputs → Block → Block outputs → wires → next Block inputs

Wire connections define execution order via topological sort on block dependency graph.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


@dataclass
class FBDPin:
    """An input or output pin on an FBD block."""
    name: str               # Pin name (e.g., "IN1", "IN2", "OUT", "Q", "ET")
    direction: str          # "input" or "output"
    variable: str = ""      # Connected variable name or literal value
    wire_source: str = ""   # Source block ID:pin for wires


@dataclass
class FBDBlock:
    """A function block in an FBD network."""
    id: str                 # Unique block ID within network
    block_type: str         # AND, OR, TON, CTU, ADD, MOVE, etc.
    instance_name: str = "" # Instance variable name (e.g., "MyTimer")
    pins: dict[str, FBDPin] = field(default_factory=dict)
    execution_order: int = 0


@dataclass
class FBDWire:
    """A wire connecting an output pin to an input pin."""
    source_block: str       # Block ID of source
    source_pin: str         # Output pin name
    target_block: str       # Block ID of target
    target_pin: str         # Input pin name


@dataclass
class FBDNetwork:
    """A single FBD network (one rung/section of logic)."""
    number: int
    title: str = ""
    blocks: dict[str, FBDBlock] = field(default_factory=dict)
    wires: list[FBDWire] = field(default_factory=list)
    input_vars: list[FBDPin] = field(default_factory=list)   # Network input terminals
    output_vars: list[FBDPin] = field(default_factory=list)  # Network output terminals


class FBDConversion(BaseModel):
    """Result of FBD-to-ST conversion."""
    st_code: str
    source_networks: list[int] = []
    warnings: list[str] = []
    conversion_notes: list[str] = []


class FBDConverter:
    """Convert Function Block Diagram (FBD) XML to Structured Text (ST).

    Handles FBD XML from Siemens, TwinCAT, CODESYS, Rockwell, and other
    IEC 61131-3 compliant tools.
    """

    # IEC 61131-3 standard function blocks (generate FB calls)
    STANDARD_FBS = {
        "TON", "TOF", "TP",                    # Timers
        "CTU", "CTD", "CTUD",                  # Counters
        "SR", "RS",                             # Latch
        "R_TRIG", "F_TRIG",                     # Edge detection
    }

    # Standard functions (generate inline expressions or function calls)
    STANDARD_FUNCTIONS = {
        # Logic (2+ inputs → 1 output)
        "AND": {"op": "AND", "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "OR":  {"op": "OR",  "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "XOR": {"op": "XOR", "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "NOT": {"op": "NOT", "inputs": ["IN"],          "output": "OUT"},
        # Arithmetic
        "ADD": {"op": "+", "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "SUB": {"op": "-", "inputs": ["IN1", "IN2"], "output": "OUT"},
        "MUL": {"op": "*", "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "DIV": {"op": "/", "inputs": ["IN1", "IN2"], "output": "OUT"},
        "MOD": {"op": "MOD", "inputs": ["IN1", "IN2"], "output": "OUT"},
        # Comparison (2 inputs → 1 boolean output)
        "GT":  {"op": ">",  "inputs": ["IN1", "IN2"], "output": "OUT"},
        "GE":  {"op": ">=", "inputs": ["IN1", "IN2"], "output": "OUT"},
        "LT":  {"op": "<",  "inputs": ["IN1", "IN2"], "output": "OUT"},
        "LE":  {"op": "<=", "inputs": ["IN1", "IN2"], "output": "OUT"},
        "EQ":  {"op": "=",  "inputs": ["IN1", "IN2"], "output": "OUT"},
        "NE":  {"op": "<>", "inputs": ["IN1", "IN2"], "output": "OUT"},
        # Selection
        "SEL": {"inputs": ["G", "IN0", "IN1"], "output": "OUT"},
        "MAX": {"op": "MAX", "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "MIN": {"op": "MIN", "inputs": ["IN1", "IN2"], "output": "OUT", "multi_input": True},
        "LIMIT": {"inputs": ["MN", "IN", "MX"], "output": "OUT"},
        # Bit operations
        "SHL": {"op": "SHL", "inputs": ["IN", "N"], "output": "OUT"},
        "SHR": {"op": "SHR", "inputs": ["IN", "N"], "output": "OUT"},
        "ROL": {"op": "ROL", "inputs": ["IN", "N"], "output": "OUT"},
        "ROR": {"op": "ROR", "inputs": ["IN", "N"], "output": "OUT"},
    }

    # Type conversion function patterns
    TYPE_CONV_PATTERN = re.compile(
        r"^(\w+)_TO_(\w+)$",
        re.IGNORECASE,
    )

    @classmethod
    def convert_xml_to_st(cls, xml_source: str) -> FBDConversion:
        """Convert FBD XML source to ST code."""
        xml_source = xml_source.strip()
        if xml_source.startswith("[FBD_XML:"):
            xml_source = xml_source[9:-1]

        try:
            root = ET.fromstring(xml_source)
        except ET.ParseError as e:
            return FBDConversion(
                st_code="",
                warnings=[f"Failed to parse FBD XML: {e}"],
            )

        networks = cls._parse_networks(root)
        if not networks:
            return FBDConversion(
                st_code="",
                warnings=["No FBD networks found in XML"],
            )

        st_parts = []
        warnings = []
        source_networks = []

        for net in networks:
            try:
                code = cls._network_to_st(net)
                if code:
                    st_parts.append(
                        f"// Network {net.number}" +
                        (f": {net.title}" if net.title else "")
                    )
                    st_parts.append(code)
                    source_networks.append(net.number)
            except Exception as e:
                warnings.append(f"Failed to convert FBD network {net.number}: {e}")

        return FBDConversion(
            st_code="\n\n".join(st_parts),
            source_networks=source_networks,
            warnings=warnings,
            conversion_notes=[
                "Converted from graphical FBD to text ST for LLM analysis",
                "Data flow preserved via topological sort of block dependency graph",
            ],
        )

    # ─── XML parsing ───────────────────────────────────────────────

    @classmethod
    def _parse_networks(cls, root: ET.Element) -> list[FBDNetwork]:
        """Parse all FBD networks from the XML tree."""
        networks = []

        for elem in root.iter():
            tag = cls._local_tag(elem)
            if tag != "Network":
                continue

            net = FBDNetwork(number=int(elem.get("Number", elem.get("ID", "0"))))

            # Extract title
            for child in elem:
                ctag = cls._local_tag(child)
                if ctag == "Title" and child.text:
                    net.title = child.text.strip()

            cls._parse_fbd_elements(elem, net)

            if net.blocks or net.input_vars or net.output_vars:
                networks.append(net)

        return networks

    @classmethod
    def _parse_fbd_elements(cls, parent: ET.Element, net: FBDNetwork):
        """Parse FBD elements (blocks, pins, wires, variables) from a network."""
        # First pass: collect all blocks
        for elem in parent.iter():
            tag = cls._local_tag(elem)

            if tag == "Block":
                block = cls._parse_block(elem)
                if block:
                    net.blocks[block.id] = block

            elif tag in ("FunctionBlock", "FB"):
                block = cls._parse_fb_element(elem, tag)
                if block:
                    net.blocks[block.id] = block

            elif tag in ("Variable", "InputVariable", "OutputVariable"):
                pin = cls._parse_variable_terminal(elem)
                if pin:
                    if tag == "OutputVariable":
                        net.output_vars.append(pin)
                    else:
                        net.input_vars.append(pin)

        # Second pass: collect wires
        for elem in parent.iter():
            tag = cls._local_tag(elem)
            if tag == "Wire":
                wire = cls._parse_wire(elem)
                if wire:
                    net.wires.append(wire)

        # Third pass: resolve wire connections to pins
        cls._resolve_wires(net)

    @classmethod
    def _parse_block(cls, elem: ET.Element) -> FBDBlock | None:
        """Parse a <Block> element (Siemens/CODESYS FBD format).

        <Block ID="1" Type="ADD" ExecutionOrder="0">
            <InputPin Name="IN1" FormalParameter="IN1">Variable1</InputPin>
            <InputPin Name="IN2" FormalParameter="IN2">100</InputPin>
            <OutputPin Name="OUT" FormalParameter="OUT">ResultVar</OutputPin>
        </Block>
        """
        block_id = elem.get("ID", elem.get("Id", elem.get("Name", "")))
        block_type = elem.get("Type", elem.get("BlockType", ""))
        if not block_id or not block_type:
            return None

        block = FBDBlock(
            id=block_id,
            block_type=block_type.upper(),
            instance_name=elem.get("InstanceName", elem.get("Instance", "")),
            execution_order=int(elem.get("ExecutionOrder", "0")),
        )

        for child in elem:
            ctag = cls._local_tag(child)
            pin_name = child.get("Name", child.get("FormalParameter", ""))
            if not pin_name:
                continue

            if ctag == "InputPin":
                pin = FBDPin(
                    name=pin_name,
                    direction="input",
                    variable=cls._get_text_or_attr(child),
                )
                block.pins[pin_name] = pin

            elif ctag == "OutputPin":
                pin = FBDPin(
                    name=pin_name,
                    direction="output",
                    variable=cls._get_text_or_attr(child),
                )
                block.pins[pin_name] = pin

        return block

    @classmethod
    def _parse_fb_element(cls, elem: ET.Element, tag: str) -> FBDBlock | None:
        """Parse a <FunctionBlock> or <FB> element (TwinCAT/extended format)."""
        block_id = elem.get("ID", elem.get("Id", elem.get("InstanceId", "")))
        block_type = elem.get("Type", elem.get("Kind", elem.get("TypeName", "")))
        if not block_id:
            return None

        block = FBDBlock(
            id=block_id,
            block_type=block_type.upper() if block_type else "UNKNOWN",
            instance_name=elem.get("InstanceName", elem.get("Name", "")),
        )

        for child in elem:
            ctag = cls._local_tag(child)
            if ctag in ("Input", "InputPin", "In"):
                pin_name = child.get("Name", child.get("FormalParameter", f"IN{len([p for p in block.pins.values() if p.direction=='input'])+1}"))
                pin = FBDPin(
                    name=pin_name,
                    direction="input",
                    variable=cls._get_text_or_attr(child),
                )
                block.pins[pin_name] = pin
            elif ctag in ("Output", "OutputPin", "Out"):
                pin_name = child.get("Name", child.get("FormalParameter", f"OUT{len([p for p in block.pins.values() if p.direction=='output'])+1}"))
                pin = FBDPin(
                    name=pin_name,
                    direction="output",
                    variable=cls._get_text_or_attr(child),
                )
                block.pins[pin_name] = pin

        return block

    @classmethod
    def _parse_variable_terminal(cls, elem: ET.Element) -> FBDPin | None:
        """Parse a variable terminal (network input/output)."""
        name = elem.get("Name", elem.get("Variable", ""))
        if not name:
            name = cls._get_text_or_attr(elem)
        if not name:
            return None

        # Determine direction from tag or position
        tag = cls._local_tag(elem)
        direction = "output" if tag == "OutputVariable" else "input"

        return FBDPin(
            name=name,
            direction=direction,
            variable=name,
        )

    @classmethod
    def _parse_wire(cls, elem: ET.Element) -> FBDWire | None:
        """Parse a <Wire> element connecting blocks.

        <Wire>
            <SourceBlock>1</SourceBlock>
            <SourcePin>OUT</SourcePin>
            <TargetBlock>2</TargetBlock>
            <TargetPin>IN1</TargetPin>
        </Wire>

        Or attribute-based:
        <Wire SourceBlock="1" SourcePin="OUT" TargetBlock="2" TargetPin="IN1"/>
        """
        src_block = elem.get("SourceBlock", "")
        src_pin = elem.get("SourcePin", "")
        tgt_block = elem.get("TargetBlock", "")
        tgt_pin = elem.get("TargetPin", "")

        for child in elem:
            ctag = cls._local_tag(child)
            text = (child.text or "").strip()
            if ctag == "SourceBlock" and text:
                src_block = text
            elif ctag == "SourcePin" and text:
                src_pin = text
            elif ctag == "TargetBlock" and text:
                tgt_block = text
            elif ctag == "TargetPin" and text:
                tgt_pin = text
            # Also handle "From"/"To" style
            elif ctag == "From":
                src_block = child.get("Block", src_block)
                src_pin = child.get("Pin", src_pin)
            elif ctag == "To":
                tgt_block = child.get("Block", tgt_block)
                tgt_pin = child.get("Pin", tgt_pin)

        if src_block and tgt_block:
            return FBDWire(
                source_block=src_block,
                source_pin=src_pin,
                target_block=tgt_block,
                target_pin=tgt_pin,
            )
        return None

    @classmethod
    def _resolve_wires(cls, net: FBDNetwork):
        """Resolve wire connections to populate pin wire_source references."""
        for wire in net.wires:
            target_block = net.blocks.get(wire.target_block)
            if target_block:
                pin = target_block.pins.get(wire.target_pin)
                if pin:
                    pin.wire_source = f"{wire.source_block}:{wire.source_pin}"

    # ─── ST code generation ────────────────────────────────────────

    @classmethod
    def _network_to_st(cls, net: FBDNetwork) -> str:
        """Convert an FBD network to ST code using topological sort."""
        if not net.blocks:
            return cls._generate_terminal_st(net)

        # Build dependency graph: block A depends on block B if B's output feeds A's input
        deps: dict[str, set[str]] = {bid: set() for bid in net.blocks}
        for wire in net.wires:
            if wire.target_block in deps and wire.source_block in net.blocks:
                deps[wire.target_block].add(wire.source_block)

        # Topological sort
        order = cls._topological_sort(deps)

        # Generate ST for each block in order
        st_lines = []
        block_results: dict[str, str] = {}  # block_id → expression for its primary output

        for bid in order:
            block = net.blocks[bid]
            code, result_expr = cls._block_to_st(block, block_results, net)
            if code:
                st_lines.append(code)
            if result_expr:
                block_results[bid] = result_expr

        # Handle output terminals
        for out_var in net.output_vars:
            # Find what feeds this output
            source_expr = cls._find_output_source(out_var, net, block_results)
            if source_expr:
                st_lines.append(f"{cls._sanitize(out_var.name)} := {source_expr};")

        return "\n".join(st_lines)

    @classmethod
    def _block_to_st(
        cls,
        block: FBDBlock,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> tuple[str, str]:
        """Generate ST code for a single block.

        Returns (code_text, result_expression).
        result_expression is used when this block's output feeds another block's input.
        """
        bt = block.block_type.upper()

        # Standard function block (timer, counter, latch, edge detect)
        if bt in cls.STANDARD_FBS:
            return cls._generate_fb_call(block, block_results, net)

        # Standard function (arithmetic, logic, comparison, etc.)
        if bt in cls.STANDARD_FUNCTIONS:
            return cls._generate_standard_function(block, bt, block_results, net)

        # Type conversion (INT_TO_REAL, WORD_TO_DWORD, etc.)
        if cls.TYPE_CONV_PATTERN.match(bt):
            return cls._generate_type_conversion(block, bt, block_results, net)

        # MOVE block
        if bt == "MOVE":
            return cls._generate_move(block, block_results, net)

        # Unknown/generic block — generate as function call
        return cls._generate_generic_block(block, block_results, net)

    @classmethod
    def _generate_fb_call(
        cls,
        block: FBDBlock,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> tuple[str, str]:
        """Generate ST call for IEC 61131-3 standard function blocks.

        Example: MyTimer(IN := sensor, PT := T#5S);
                 is_running := MyTimer.Q;
                 elapsed := MyTimer.ET;
        """
        bt = block.block_type.upper()
        instance = block.instance_name or f"fb_{block.id}"

        # Build parameter list
        params = []
        for pin in block.pins.values():
            if pin.direction == "input" and pin.name not in ("EN", "ENO"):
                value = cls._resolve_input(pin, block_results, net)
                params.append(f"{pin.name} := {value}")

        call_line = f"{instance}({', '.join(params)});"
        st_lines = [call_line]

        # Map output pins to instance properties
        for pin in block.pins.values():
            if pin.direction == "output":
                # Standard output names for FBs
                if pin.name in ("Q", "OUT"):
                    st_lines.append(f"{cls._sanitize(pin.variable)} := {instance}.Q;" if pin.variable else "")
                elif pin.name == "ET":
                    st_lines.append(f"{cls._sanitize(pin.variable)} := {instance}.ET;" if pin.variable else "")
                elif pin.name == "CV":
                    st_lines.append(f"{cls._sanitize(pin.variable)} := {instance}.CV;" if pin.variable else "")

        code = "\n".join(line for line in st_lines if line)
        # Result expression for downstream blocks
        primary_output = block.pins.get("Q") or block.pins.get("OUT") or block.pins.get("ET")
        result = f"{instance}.{primary_output.name}" if primary_output else instance
        return code, result

    @classmethod
    def _generate_standard_function(
        cls,
        block: FBDBlock,
        func_name: str,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> tuple[str, str]:
        """Generate ST for a standard IEC function (ADD, AND, GT, etc.).

        For 2-input operators with a connected variable output, generates:
            result_var := input1 OP input2;
        For blocks feeding other blocks, returns the expression.
        """
        spec = cls.STANDARD_FUNCTIONS[func_name]
        op = spec.get("op", func_name)
        input_names = spec["inputs"]
        output_name = spec["output"]
        is_multi = spec.get("multi_input", False)

        # Collect input values in order
        input_values = []
        for pin_name in input_names:
            pin = block.pins.get(pin_name)
            if pin:
                input_values.append(cls._resolve_input(pin, block_results, net))
            else:
                # Check for extra input pins (IN3, IN4, etc. for multi-input)
                pass

        # Check for additional inputs (IN3, IN4, ...) for multi-input functions
        if is_multi:
            idx = 3
            while True:
                extra_pin = block.pins.get(f"IN{idx}")
                if extra_pin:
                    input_values.append(cls._resolve_input(extra_pin, block_results, net))
                    idx += 1
                else:
                    break

        if not input_values:
            return "", ""

        # Build expression
        if func_name == "NOT":
            expr = f"NOT ({input_values[0]})"
        elif op in ("+", "-", "*", "/", "MOD", "AND", "OR", "XOR"):
            expr = f" {op} ".join(f"({v})" for v in input_values)
            if len(input_values) > 1:
                expr = f"({expr})" if op in ("AND", "OR", "XOR") else expr
        elif op in (">", ">=", "<", "<=", "=", "<>"):
            expr = f"({input_values[0]}) {op} ({input_values[1]})"
        elif func_name in ("SHL", "SHR", "ROL", "ROR"):
            expr = f"{func_name}({input_values[0]}, N := {input_values[1]})"
        elif func_name in ("MAX", "MIN"):
            expr = f"{func_name}({', '.join(input_values)})"
        elif func_name == "LIMIT":
            expr = f"LIMIT({input_values[0]}, {input_values[1]}, {input_values[2]})"
        elif func_name == "SEL":
            expr = f"SEL({input_values[0]}, {input_values[1]}, {input_values[2]})"
        else:
            expr = f"{func_name}({', '.join(input_values)})"

        # Output assignment
        out_pin = block.pins.get(output_name)
        if out_pin and out_pin.variable:
            code = f"{cls._sanitize(out_pin.variable)} := {expr};"
            return code, cls._sanitize(out_pin.variable)
        else:
            # No named output — expression feeds another block
            return "", expr

    @classmethod
    def _generate_type_conversion(
        cls,
        block: FBDBlock,
        func_name: str,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> tuple[str, str]:
        """Generate ST for type conversion (INT_TO_REAL, etc.)."""
        in_pin = block.pins.get("IN") or block.pins.get("IN1")
        out_pin = block.pins.get("OUT")

        if not in_pin:
            return "", ""

        in_val = cls._resolve_input(in_pin, block_results, net)
        expr = f"{func_name}({in_val})"

        if out_pin and out_pin.variable:
            code = f"{cls._sanitize(out_pin.variable)} := {expr};"
            return code, cls._sanitize(out_pin.variable)
        return "", expr

    @classmethod
    def _generate_move(
        cls,
        block: FBDBlock,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> tuple[str, str]:
        """Generate ST for MOVE block."""
        in_pin = block.pins.get("IN") or block.pins.get("IN1")
        out_pin = block.pins.get("OUT")

        if not in_pin:
            return "", ""

        in_val = cls._resolve_input(in_pin, block_results, net)

        if out_pin and out_pin.variable:
            code = f"{cls._sanitize(out_pin.variable)} := {in_val};"
            return code, cls._sanitize(out_pin.variable)
        return "", in_val

    @classmethod
    def _generate_generic_block(
        cls,
        block: FBDBlock,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> tuple[str, str]:
        """Generate ST for a custom/unknown block as a function call."""
        bt = block.block_type
        instance = block.instance_name or f"fb_{block.id}"

        params = []
        for pin in block.pins.values():
            if pin.direction == "input":
                value = cls._resolve_input(pin, block_results, net)
                params.append(f"{pin.name} := {value}")
            elif pin.direction == "output" and pin.variable:
                params.append(f"{pin.name} => {cls._sanitize(pin.variable)}")

        call_line = f"{instance}({', '.join(params)});"
        return call_line, instance

    @classmethod
    def _generate_terminal_st(cls, net: FBDNetwork) -> str:
        """Generate ST for a network with only terminals (simple assignments)."""
        lines = []
        # If there are input and output vars with direct connections
        for out in net.output_vars:
            for inp in net.input_vars:
                # Simple pass-through
                lines.append(f"{cls._sanitize(out.name)} := {cls._sanitize(inp.name)};")
        return "\n".join(lines)

    # ─── Helper methods ────────────────────────────────────────────

    @classmethod
    def _resolve_input(
        cls,
        pin: FBDPin,
        block_results: dict[str, str],
        net: FBDNetwork,
    ) -> str:
        """Resolve an input pin to its value expression.

        If the pin is connected via wire to another block's output, use that block's
        result expression. If it has a direct variable/literal, use that.
        """
        if pin.wire_source:
            src_id, src_pin = pin.wire_source.split(":", 1)
            if src_id in block_results:
                return block_results[src_id]

        # Direct variable or literal
        if pin.variable:
            return cls._resolve_variable(pin.variable, net)

        return "0"  # Default

    @classmethod
    def _resolve_variable(cls, var_name: str, net: FBDNetwork) -> str:
        """Resolve a variable name, checking if it's a literal or a variable."""
        if not var_name:
            return "0"

        # Check if it's a numeric literal
        try:
            float(var_name)
            return var_name
        except ValueError:
            pass

        # Check for time literals (T#5S, TIME#5s, etc.)
        if re.match(r"^(T|TIME|TIMEOFDAY|TOD|DATE|DT|DTIME)#", var_name, re.IGNORECASE):
            return var_name

        # Check for string literals
        if var_name.startswith("'") or var_name.startswith('"'):
            return var_name

        # Check for boolean literals
        if var_name.upper() in ("TRUE", "FALSE"):
            return var_name.upper()

        # It's a variable — sanitize
        return cls._sanitize(var_name)

    @classmethod
    def _find_output_source(
        cls,
        out_var: FBDPin,
        net: FBDNetwork,
        block_results: dict[str, str],
    ) -> str | None:
        """Find the expression that feeds an output terminal."""
        # Check wires targeting this output variable
        for wire in net.wires:
            if wire.target_block == out_var.name or wire.target_pin == out_var.name:
                if wire.source_block in block_results:
                    return block_results[wire.source_block]

        # Check if out_var.variable matches any block output
        for block in net.blocks.values():
            for pin in block.pins.values():
                if pin.direction == "output" and pin.variable == out_var.variable:
                    if block.id in block_results:
                        return block_results[block.id]

        return None

    @classmethod
    def _topological_sort(cls, deps: dict[str, set[str]]) -> list[str]:
        """Topological sort of block dependency graph (Kahn's algorithm)."""
        in_degree = {n: 0 for n in deps}
        for node, dep_set in deps.items():
            for dep in dep_set:
                if dep in in_degree:
                    in_degree[node] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        result = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for other, dep_set in deps.items():
                if node in dep_set:
                    in_degree[other] -= 1
                    if in_degree[other] == 0:
                        queue.append(other)

        # If not all nodes processed, there's a cycle — add remaining in original order
        if len(result) < len(deps):
            for n in deps:
                if n not in result:
                    result.append(n)

        return result

    @classmethod
    def _sanitize(cls, name: str) -> str:
        """Sanitize a variable name for ST output."""
        if not name:
            return "unnamed"

        # Remove AT address syntax
        name = re.sub(r"AT\s+", "", name, flags=re.IGNORECASE)

        # Quote names with special characters
        if re.search(r"[^A-Za-z0-9_.]", name):
            return f'"{name}"'

        return name

    @classmethod
    def _local_tag(cls, elem: ET.Element) -> str:
        """Get the local tag name, stripping namespace."""
        tag = elem.tag
        if "}" in tag:
            return tag.split("}")[1]
        return tag

    @classmethod
    def _get_text_or_attr(cls, elem: ET.Element) -> str:
        """Get value from element text or common attributes."""
        text = (elem.text or "").strip()
        if text:
            return text
        for attr in ("Value", "Variable", "Constant", "Literal"):
            val = elem.get(attr)
            if val:
                return val
        return ""

    @classmethod
    def has_fbd_marker(cls, source_code: str) -> bool:
        """Check if source code contains FBD XML markers."""
        return bool(re.search(r"\[FBD_XML:", source_code))

    @classmethod
    def extract_and_convert(cls, source_code: str) -> FBDConversion:
        """Extract FBD XML from marked source and convert to ST."""
        pattern = r"\[FBD_XML:(.*?)\]"
        matches = list(re.finditer(pattern, source_code, re.DOTALL))

        if not matches:
            return FBDConversion(st_code=source_code)

        all_st = []
        all_warnings = []
        all_networks = []

        for match in matches:
            xml_content = match.group(1)
            result = cls.convert_xml_to_st(xml_content)
            if result.st_code:
                all_st.append("// [FBD → ST conversion]")
                all_st.append(result.st_code)
            all_warnings.extend(result.warnings)
            all_networks.extend(result.source_networks)

        return FBDConversion(
            st_code="\n\n".join(all_st),
            source_networks=all_networks,
            warnings=all_warnings,
            conversion_notes=[f"Converted {len(matches)} FBD network(s) to ST"],
        )
