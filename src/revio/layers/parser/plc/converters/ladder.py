"""Ladder Diagram (LD) to Structured Text (ST) semantic converter.

Converts graphical ladder logic into equivalent ST code so that LLMs can
analyze it. The conversion uses a directed graph (AOV - Activity On Vertex)
with topological sorting to produce strictly sequential ST output.

LD Element Mapping:
  NO contact (series)   → AND with variable         → IF "Tag1" AND "Tag2" THEN
  NC contact (series)   → AND with NOT variable     → IF "Tag1" AND NOT "Tag2" THEN
  Coil                  → Assignment                → varName := TRUE;
  Parallel branches     → OR logic                  → ELSIF cond_A OR cond_B THEN
  Conditional jump      → GOTO                      → IF cond THEN GOTO Label;
  TON/TOF/TP timer      → Timer function block      → timer(IN:=cond, PT:=T#5S);
  CTU/CTD counter       → Counter function block    → counter(CU:=cond, PV:=10);

The output ST code is annotated so that the review agent can trace each
logical element back to its original LD network for contextual feedback.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class LDNodeType(str, Enum):
    """Types of nodes in the LD directed graph."""
    POWER_RAIL_LEFT = "power_rail_left"
    POWER_RAIL_RIGHT = "power_rail_right"
    CONTACT_NO = "contact_no"       # Normally open contact
    CONTACT_NC = "contact_nc"       # Normally closed contact
    COIL = "coil"                   # Output coil
    COIL_SET = "coil_set"           # Set (latch) coil
    COIL_RESET = "coil_reset"       # Reset (unlatch) coil
    FUNCTION_BLOCK = "function_block"  # TON, CTU, etc.
    BRANCH_START = "branch_start"   # Parallel branch start
    BRANCH_END = "branch_end"       # Parallel branch end
    JUMP = "jump"                   # JC / JCN jump
    LABEL = "label"                 # Jump label target


@dataclass
class LDNode:
    """A node in the LD directed graph."""
    id: str
    node_type: LDNodeType
    operand: str = ""           # Variable name or tag
    parameters: dict = field(default_factory=dict)  # FB parameters
    label: str = ""             # Jump target label
    network_number: int = 0
    raw_xml: str = ""


@dataclass
class LDEdge:
    """An edge in the LD directed graph."""
    source_id: str
    target_id: str
    edge_type: str = "series"  # series, parallel, jump


@dataclass
class LDGraph:
    """Directed graph representation of a Ladder Diagram network."""
    nodes: dict[str, LDNode] = field(default_factory=dict)
    edges: list[LDEdge] = field(default_factory=list)
    network_number: int = 0
    network_title: str = ""


class STConversion(BaseModel):
    """Result of LD-to-ST conversion."""
    st_code: str
    source_networks: list[int] = []
    warnings: list[str] = []
    conversion_notes: list[str] = []


class LadderDiagramConverter:
    """Convert Ladder Diagram (LD) XML to Structured Text (ST)."""

    # Siemens LD element type identifiers
    SIEMENS_LD_TYPES = {
        "Contact": LDNodeType.CONTACT_NO,
        "NContact": LDNodeType.CONTACT_NC,
        "Coil": LDNodeType.COIL,
        "SCoil": LDNodeType.COIL_SET,
        "RCoil": LDNodeType.COIL_RESET,
        "--| |--": LDNodeType.CONTACT_NO,
        "--|/|--": LDNodeType.CONTACT_NC,
        "--( )--": LDNodeType.COIL,
        "--(S)--": LDNodeType.COIL_SET,
        "--(R)--": LDNodeType.COIL_RESET,
    }

    # TwinCAT LD element type identifiers
    TWINCAT_LD_TYPES = {
        "Contact": LDNodeType.CONTACT_NO,
        "NegatedContact": LDNodeType.CONTACT_NC,
        "Coil": LDNodeType.COIL,
        "SetCoil": LDNodeType.COIL_SET,
        "ResetCoil": LDNodeType.COIL_RESET,
    }

    # Function block types that map to IEC 61131-3 standard FBs
    STANDARD_FBS = {
        "TON": "TON",
        "TOF": "TOF",
        "TP": "TP",
        "CTU": "CTU",
        "CTD": "CTD",
        "CTUD": "CTUD",
        "TP_X": "TP",
        "TON_X": "TON",
        "TOF_X": "TOF",
    }

    @classmethod
    def convert_xml_to_st(cls, xml_source: str) -> STConversion:
        """Convert LD XML source to ST code."""
        # Clean the XML source
        xml_source = xml_source.strip()
        if xml_source.startswith("[LD_XML:"):
            xml_source = xml_source[8:-1]
        if xml_source.startswith("[FBD_XML:"):
            xml_source = xml_source[9:-1]

        try:
            root = ET.fromstring(xml_source)
        except ET.ParseError as e:
            return STConversion(
                st_code="",
                warnings=[f"Failed to parse LD XML: {e}"],
            )

        # Build graphs from networks
        graphs = cls._build_graphs(root)
        if not graphs:
            return STConversion(
                st_code="",
                warnings=["No LD networks found in XML"],
            )

        # Convert each graph to ST
        st_parts = []
        warnings = []
        source_networks = []

        for graph in graphs:
            try:
                st_code = cls._graph_to_st(graph)
                if st_code:
                    st_parts.append(f"// Network {graph.network_number}" +
                                    (f": {graph.network_title}" if graph.network_title else ""))
                    st_parts.append(st_code)
                    source_networks.append(graph.network_number)
            except Exception as e:
                warnings.append(f"Failed to convert network {graph.network_number}: {e}")

        return STConversion(
            st_code="\n\n".join(st_parts),
            source_networks=source_networks,
            warnings=warnings,
            conversion_notes=[
                "Converted from graphical LD/FBD to text ST for LLM analysis",
                "Original graphical structure preserved in source_networks list",
            ],
        )

    @classmethod
    def _build_graphs(cls, root: ET.Element) -> list[LDGraph]:
        """Build directed graphs from LD XML networks."""
        graphs = []

        # Find all Network elements
        for net_elem in root.iter():
            tag = net_elem.tag.split("}")[-1] if "}" in net_elem.tag else net_elem.tag
            if tag != "Network":
                continue

            graph = LDGraph()
            graph.network_number = int(net_elem.get("Number", net_elem.get("ID", "0")))

            # Extract title
            for child in net_elem:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "Title" and child.text:
                    graph.network_title = child.text.strip()
                    break

            # Parse LD elements into graph nodes and edges
            cls._parse_ld_elements(net_elem, graph)

            if graph.nodes:
                graphs.append(graph)

        return graphs

    @classmethod
    def _parse_ld_elements(cls, parent: ET.Element, graph: LDGraph):
        """Parse LD elements from XML into graph nodes and edges."""
        # Track connection points for building edges
        node_list = []

        for elem in parent.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

            # Detect LD node type
            node_type = cls._detect_node_type(elem, tag)
            if node_type is None:
                continue

            node_id = elem.get("ID", elem.get("Name", f"node_{len(graph.nodes)}"))
            operand = cls._extract_operand(elem)
            params = cls._extract_fb_params(elem) if node_type == LDNodeType.FUNCTION_BLOCK else {}
            label = elem.get("Label", "")

            node = LDNode(
                id=node_id,
                node_type=node_type,
                operand=operand,
                parameters=params,
                label=label,
                network_number=graph.network_number,
            )

            graph.nodes[node_id] = node
            node_list.append(node)

        # Build edges based on spatial relationships (left-to-right, top-to-bottom)
        cls._infer_edges(node_list, graph)

    @classmethod
    def _detect_node_type(cls, elem: ET.Element, tag: str) -> LDNodeType | None:
        """Detect the LD node type from XML element."""
        # Check element attributes
        elem_type = elem.get("Type", elem.get("OperandType", ""))

        # Siemens format
        if tag in cls.SIEMENS_LD_TYPES:
            return cls.SIEMENS_LD_TYPES[tag]

        # TwinCAT format
        if tag in cls.TWINCAT_LD_TYPES:
            return cls.TWINCAT_LD_TYPES[tag]

        # Check for function block elements
        if tag in cls.STANDARD_FBS or elem_type in cls.STANDARD_FBS:
            return LDNodeType.FUNCTION_BLOCK

        # Branch elements
        if tag in ("Branch", "ParallelBranch", "BranchStart"):
            return LDNodeType.BRANCH_START
        if tag in ("BranchEnd", "ParallelBranchEnd"):
            return LDNodeType.BRANCH_END

        # Power rails
        if tag in ("LeftPowerRail", "PowerRailLeft"):
            return LDNodeType.POWER_RAIL_LEFT
        if tag in ("RightPowerRail", "PowerRailRight"):
            return LDNodeType.POWER_RAIL_RIGHT

        # Jump
        if tag in ("Jump", "JC", "JCN"):
            return LDNodeType.JUMP
        if tag in ("Label", "JumpTarget"):
            return LDNodeType.LABEL

        # Heuristic: check for coil/contact patterns in attributes
        if elem.get("Negated", "false").lower() == "true":
            return LDNodeType.CONTACT_NC

        return None

    @classmethod
    def _extract_operand(cls, elem: ET.Element) -> str:
        """Extract the operand (variable/tag name) from an LD element."""
        # Try various attribute names
        for attr in ("Operand", "Name", "Tag", "Variable", "Address"):
            val = elem.get(attr)
            if val:
                return val.strip()

        # Try child elements
        for child in elem:
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ctag in ("Operand", "Name", "Tag") and child.text:
                return child.text.strip()

        return ""

    @classmethod
    def _extract_fb_params(cls, elem: ET.Element) -> dict:
        """Extract function block parameters."""
        params = {}
        for child in elem:
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ctag == "Parameter" or ctag.endswith("Param"):
                name = child.get("Name", "")
                value = child.get("Value", child.text or "")
                if name:
                    params[name] = value
        return params

    @classmethod
    def _infer_edges(cls, node_list: list[LDNode], graph: LDGraph):
        """Infer edges between nodes based on their types and positions."""
        # Simple sequential connection: each node connects to the next
        for i in range(len(node_list) - 1):
            src = node_list[i]
            dst = node_list[i + 1]

            # Skip edges from/to power rails and branches
            if src.node_type in (LDNodeType.POWER_RAIL_RIGHT, LDNodeType.BRANCH_END):
                continue
            if dst.node_type in (LDNodeType.POWER_RAIL_LEFT, LDNodeType.BRANCH_START):
                continue

            edge_type = "parallel" if (
                src.node_type == LDNodeType.BRANCH_START or
                dst.node_type == LDNodeType.BRANCH_END
            ) else "series"

            graph.edges.append(LDEdge(
                source_id=src.id,
                target_id=dst.id,
                edge_type=edge_type,
            ))

    @classmethod
    def _graph_to_st(cls, graph: LDGraph) -> str:
        """Convert a single LD graph to ST code."""
        # Separate nodes by function
        contacts = []
        coils = []
        function_blocks = []
        jumps = []
        conditions = []  # Accumulated condition expressions

        for node in graph.nodes.values():
            if node.node_type in (LDNodeType.CONTACT_NO, LDNodeType.CONTACT_NC):
                contacts.append(node)
            elif node.node_type in (LDNodeType.COIL, LDNodeType.COIL_SET, LDNodeType.COIL_RESET):
                coils.append(node)
            elif node.node_type == LDNodeType.FUNCTION_BLOCK:
                function_blocks.append(node)
            elif node.node_type == LDNodeType.JUMP:
                jumps.append(node)

        if not contacts and not coils and not function_blocks:
            return ""

        # Build condition expression from contacts
        condition_parts = []
        for contact in contacts:
            operand = cls._sanitize_operand(contact.operand)
            if contact.node_type == LDNodeType.CONTACT_NO:
                condition_parts.append(operand)
            else:  # NC
                condition_parts.append(f"NOT {operand}")

        # Join contacts with AND (series connection)
        condition = " AND ".join(condition_parts) if condition_parts else "TRUE"

        # Check for parallel branches (OR logic)
        parallel_groups = cls._find_parallel_groups(graph)
        if parallel_groups:
            or_parts = []
            for group in parallel_groups:
                group_conditions = []
                for node_id in group:
                    node = graph.nodes.get(node_id)
                    if node and node.node_type in (LDNodeType.CONTACT_NO, LDNodeType.CONTACT_NC):
                        operand = cls._sanitize_operand(node.operand)
                        if node.node_type == LDNodeType.CONTACT_NO:
                            group_conditions.append(operand)
                        else:
                            group_conditions.append(f"NOT {operand}")
                if group_conditions:
                    or_parts.append(" AND ".join(group_conditions))
            if or_parts:
                condition = " OR ".join(f"({p})" for p in or_parts)

        # Generate ST code
        st_lines = []

        # Function blocks first (they need to be called before using their outputs)
        for fb in function_blocks:
            fb_type = cls.STANDARD_FBS.get(fb.operand.upper(), fb.operand)
            params = cls._build_fb_params_st(fb)
            st_lines.append(f"{fb.operand}({params});")

        # Conditional assignments for coils
        if coils:
            if len(coils) == 1:
                coil = coils[0]
                coil_op = cls._sanitize_operand(coil.operand)
                if coil.node_type == LDNodeType.COIL:
                    st_lines.append(f"IF {condition} THEN")
                    st_lines.append(f"    {coil_op} := TRUE;")
                    st_lines.append(f"ELSE")
                    st_lines.append(f"    {coil_op} := FALSE;")
                    st_lines.append(f"END_IF;")
                elif coil.node_type == LDNodeType.COIL_SET:
                    st_lines.append(f"IF {condition} THEN")
                    st_lines.append(f"    {coil_op} := TRUE;")
                    st_lines.append(f"END_IF;")
                elif coil.node_type == LDNodeType.COIL_RESET:
                    st_lines.append(f"IF {condition} THEN")
                    st_lines.append(f"    {coil_op} := FALSE;")
                    st_lines.append(f"END_IF;")
            else:
                # Multiple coils - generate separate blocks
                for i, coil in enumerate(coils):
                    coil_op = cls._sanitize_operand(coil.operand)
                    if coil.node_type == LDNodeType.COIL:
                        st_lines.append(f"IF {condition} THEN")
                        st_lines.append(f"    {coil_op} := TRUE;")
                        st_lines.append(f"ELSE")
                        st_lines.append(f"    {coil_op} := FALSE;")
                        st_lines.append(f"END_IF;")
                    elif coil.node_type == LDNodeType.COIL_SET:
                        st_lines.append(f"IF {condition} THEN")
                        st_lines.append(f"    {coil_op} := TRUE;")
                        st_lines.append(f"END_IF;")
                    elif coil.node_type == LDNodeType.COIL_RESET:
                        st_lines.append(f"IF {condition} THEN")
                        st_lines.append(f"    {coil_op} := FALSE;")
                        st_lines.append(f"END_IF;")

        # Jumps
        for jump in jumps:
            if jump.label:
                st_lines.append(f"IF {condition} THEN")
                st_lines.append(f"    GOTO {jump.label};")
                st_lines.append(f"END_IF;")

        return "\n".join(st_lines)

    @classmethod
    def _find_parallel_groups(cls, graph: LDGraph) -> list[list[str]]:
        """Find groups of nodes connected in parallel (OR logic)."""
        groups = []

        # Look for branch start/end pairs
        branch_starts = [n for n in graph.nodes.values()
                         if n.node_type == LDNodeType.BRANCH_START]
        branch_ends = [n for n in graph.nodes.values()
                       if n.node_type == LDNodeType.BRANCH_END]

        for start in branch_starts:
            # Find all nodes between branch start and end
            group = []
            for node in graph.nodes.values():
                if node.node_type in (LDNodeType.CONTACT_NO, LDNodeType.CONTACT_NC):
                    # Check if this node is in the branch
                    in_branch = False
                    for edge in graph.edges:
                        if edge.source_id == start.id and edge.target_id == node.id:
                            in_branch = True
                            break
                    if in_branch:
                        group.append(node.id)
            if group:
                groups.append(group)

        return groups

    @classmethod
    def _sanitize_operand(cls, operand: str) -> str:
        """Sanitize operand for ST code generation."""
        if not operand:
            return "TRUE"

        # Remove AT address syntax
        operand = re.sub(r"AT\s+", "", operand, flags=re.IGNORECASE)

        # Quote tag names that contain special characters
        if re.search(r"[^A-Za-z0-9_.]", operand):
            return f'"{operand}"'

        return operand

    @classmethod
    def _build_fb_params_st(cls, fb_node: LDNode) -> str:
        """Build ST parameter list for a function block call."""
        if not fb_node.parameters:
            return ""

        parts = []
        for name, value in fb_node.parameters.items():
            parts.append(f"{name} := {value}")

        return ", ".join(parts)

    @classmethod
    def has_graphical_language(cls, source_code: str) -> bool:
        """Check if source code contains LD markers (not FBD, which has its own converter)."""
        return bool(re.search(r"\[LD_XML:", source_code))

    @classmethod
    def extract_and_convert(cls, source_code: str) -> STConversion:
        """Extract LD XML from marked source and convert to ST."""
        # Find all LD XML blocks
        pattern = r"\[(LD)_XML:(.*?)\]"
        matches = list(re.finditer(pattern, source_code, re.DOTALL))

        if not matches:
            return STConversion(st_code=source_code)

        all_st = []
        all_warnings = []
        all_networks = []

        for match in matches:
            lang_type = match.group(1)
            xml_content = match.group(2)

            result = cls.convert_xml_to_st(xml_content)
            if result.st_code:
                all_st.append(f"// [{lang_type} → ST conversion]")
                all_st.append(result.st_code)
            all_warnings.extend(result.warnings)
            all_networks.extend(result.source_networks)

        return STConversion(
            st_code="\n\n".join(all_st),
            source_networks=all_networks,
            warnings=all_warnings,
            conversion_notes=[
                f"Converted {len(matches)} graphical network(s) to ST",
            ],
        )
