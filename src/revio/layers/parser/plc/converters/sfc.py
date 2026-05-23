"""Sequential Function Chart (SFC) to Structured Text (ST) converter.

Converts SFC XML representation into equivalent ST state machine code
using CASE statements. SFC is the IEC 61131-3 graphical language for
describing sequential control flows, widely used in batch processes,
packaging machines, and automated production lines.

SFC Elements:
  Step          → State in the state machine (enum value)
  Transition    → Condition to advance from one step to the next
  Action        → Code executed while a step is active
  Divergence    → Parallel branch start (AND-split)
  Convergence   → Parallel branch end (AND-join)

Action Qualifiers (IEC 61131-3):
  N  (Non-stored)     → Active only while step is active
  P  (Pulse)          → Executes once when step becomes active
  S  (Set/Stored)     → Activates and remains active until reset
  R  (Reset)          → Deactivates a stored action
  L  (Limited)        → Active for a limited time duration
  D  (Delayed)        → Activates after a delay
  P1 (Pulse on entry) → One-shot on step activation
  P0 (Pulse on exit)  → One-shot on step deactivation

Generated ST structure:
  TYPE E_Step : (
    Step_Initial,
    Step_Heating,
    Step_Cooling,
    Step_Done
  );
  END_TYPE

  VAR
    eCurrentStep : E_Step := Step_Initial;
    ePreviousStep : E_Step;
  END_VAR

  // SFC State Machine
  ePreviousStep := eCurrentStep;
  CASE eCurrentStep OF
    Step_Initial:
      // Actions for Step_Initial
      o_StartPump := TRUE;
      // Transition condition
      IF i_Temperature > 50 THEN
        eCurrentStep := Step_Heating;
      END_IF;
    Step_Heating:
      ...
  END_CASE;
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ActionQualifier(str, Enum):
    """IEC 61131-3 SFC action qualifiers."""
    N = "N"    # Non-stored (active while step is active)
    P = "P"    # Pulse (once on step activation)
    S = "S"    # Set/Stored (stays active until reset)
    R = "R"    # Reset (deactivates stored action)
    L = "L"    # Limited (active for limited time)
    D = "D"    # Delayed (activates after delay)
    P1 = "P1"  # Pulse on activation
    P0 = "P0"  # Pulse on deactivation


@dataclass
class SFCAction:
    """An action associated with an SFC step."""
    name: str
    qualifier: ActionQualifier = ActionQualifier.N
    body: str = ""  # ST code to execute
    duration: str = ""  # For L qualifier: time limit
    delay: str = ""  # For D qualifier: delay time


@dataclass
class SFCStep:
    """A step (state) in the SFC."""
    name: str
    is_initial: bool = False
    actions: list[SFCAction] = field(default_factory=list)
    comment: str = ""


@dataclass
class SFCTransition:
    """A transition between steps."""
    name: str = ""
    condition: str = ""  # ST expression
    from_step: str = ""
    to_step: str = ""


@dataclass
class SFCDivergence:
    """A parallel divergence (AND-split)."""
    name: str = ""
    from_step: str = ""
    to_steps: list[str] = field(default_factory=list)


@dataclass
class SFCConvergence:
    """A parallel convergence (AND-join)."""
    name: str = ""
    from_steps: list[str] = field(default_factory=list)
    to_step: str = ""


@dataclass
class SFCGraph:
    """Complete SFC graph representation."""
    name: str = ""
    steps: dict[str, SFCStep] = field(default_factory=dict)
    transitions: list[SFCTransition] = field(default_factory=list)
    divergences: list[SFCDivergence] = field(default_factory=list)
    convergences: list[SFCConvergence] = field(default_factory=list)
    initial_step: str = ""


class SFCConversion(BaseModel):
    """Result of SFC-to-ST conversion."""
    st_code: str
    step_enum_type: str = ""
    warnings: list[str] = []
    conversion_notes: list[str] = []


class SFCConverter:
    """Convert Sequential Function Chart (SFC) XML to Structured Text (ST)."""

    @classmethod
    def convert_xml_to_st(cls, xml_source: str) -> SFCConversion:
        """Convert SFC XML source to ST state machine code."""
        xml_source = xml_source.strip()
        if xml_source.startswith("[SFC_XML:"):
            xml_source = xml_source[9:-1]

        try:
            root = ET.fromstring(xml_source)
        except ET.ParseError as e:
            return SFCConversion(
                st_code="",
                warnings=[f"Failed to parse SFC XML: {e}"],
            )

        # Build SFC graph
        graph = cls._build_graph(root)
        if not graph or not graph.steps:
            return SFCConversion(
                st_code="",
                warnings=["No SFC steps found in XML"],
            )

        # Convert to ST
        return cls._graph_to_st(graph)

    @classmethod
    def _build_graph(cls, root: ET.Element) -> SFCGraph | None:
        """Build SFC graph from XML elements."""
        graph = SFCGraph()
        root_tag = cls._local_tag(root.tag)

        # Detect SFC container element
        if root_tag in ("SFC", "SequentialFunctionChart", "SFCContent"):
            container = root
            graph.name = root.get("Name", "")
        else:
            # Search for SFC container
            container = None
            for elem in root.iter():
                tag = cls._local_tag(elem.tag)
                if tag in ("SFC", "SequentialFunctionChart", "SFCContent"):
                    container = elem
                    break
            if container is None:
                container = root

        # Parse steps
        for elem in container.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "Step":
                step = cls._parse_step(elem)
                if step:
                    graph.steps[step.name] = step
                    if step.is_initial:
                        graph.initial_step = step.name

        # Parse transitions
        for elem in container.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "Transition":
                trans = cls._parse_transition(elem)
                if trans:
                    graph.transitions.append(trans)

        # Parse divergences (parallel branch start)
        for elem in container.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("Divergence", "ParallelDivergence", "AND_Divergence"):
                div = cls._parse_divergence(elem)
                if div:
                    graph.divergences.append(div)

        # Parse convergences (parallel branch end)
        for elem in container.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("Convergence", "ParallelConvergence", "AND_Convergence"):
                conv = cls._parse_convergence(elem)
                if conv:
                    graph.convergences.append(conv)

        # If no initial step found, use the first step
        if not graph.initial_step and graph.steps:
            graph.initial_step = next(iter(graph.steps))

        return graph if graph.steps else None

    @classmethod
    def _parse_step(cls, elem: ET.Element) -> SFCStep | None:
        """Parse a Step element."""
        name = elem.get("Name", elem.get("ID", ""))
        if not name:
            return None

        is_initial = elem.get("Initial", elem.get("InitialStep", "false")).lower() == "true"

        step = SFCStep(
            name=name,
            is_initial=is_initial,
            comment=elem.get("Comment", ""),
        )

        # Parse actions associated with this step
        for child in elem:
            tag = cls._local_tag(child.tag)
            if tag == "Action":
                action = cls._parse_action(child)
                if action:
                    step.actions.append(action)
            elif tag == "Actions":
                for action_elem in child:
                    a_tag = cls._local_tag(action_elem.tag)
                    if a_tag == "Action":
                        action = cls._parse_action(action_elem)
                        if action:
                            step.actions.append(action)

        return step

    @classmethod
    def _parse_action(cls, elem: ET.Element) -> SFCAction | None:
        """Parse an Action element."""
        name = elem.get("Name", "")
        if not name:
            return None

        qualifier = elem.get("Qualifier", elem.get("Qual", "N"))
        try:
            qual = ActionQualifier(qualifier.upper())
        except ValueError:
            qual = ActionQualifier.N

        # Extract action body (ST code)
        body = ""
        for child in elem:
            tag = cls._local_tag(child.tag)
            if tag in ("Body", "Code", "ST", "Implementation"):
                body = cls._extract_cdata(child)
                break

        # If no separate body, check for inline code
        if not body:
            body = cls._extract_cdata(elem)

        return SFCAction(
            name=name,
            qualifier=qual,
            body=body,
            duration=elem.get("Duration", ""),
            delay=elem.get("Delay", ""),
        )

    @classmethod
    def _parse_transition(cls, elem: ET.Element) -> SFCTransition | None:
        """Parse a Transition element."""
        name = elem.get("Name", "")

        # Get from/to step references
        from_step = elem.get("From", elem.get("FromStep", ""))
        to_step = elem.get("To", elem.get("ToStep", ""))

        # Try to find from/to from nested elements
        if not from_step or not to_step:
            for child in elem:
                tag = cls._local_tag(child.tag)
                if tag in ("From", "FromStep"):
                    from_step = from_step or child.text or child.get("Ref", "")
                elif tag in ("To", "ToStep"):
                    to_step = to_step or child.text or child.get("Ref", "")

        # Get condition
        condition = ""
        for child in elem:
            tag = cls._local_tag(child.tag)
            if tag in ("Condition", "Guard", "Expression"):
                condition = cls._extract_cdata(child)
                break

        # If condition is inline text
        if not condition:
            condition = cls._extract_cdata(elem)

        return SFCTransition(
            name=name,
            condition=condition.strip(),
            from_step=from_step,
            to_step=to_step,
        )

    @classmethod
    def _parse_divergence(cls, elem: ET.Element) -> SFCDivergence | None:
        """Parse a Divergence (parallel branch start) element."""
        name = elem.get("Name", "")
        from_step = elem.get("From", elem.get("FromStep", ""))

        to_steps = []
        for child in elem:
            tag = cls._local_tag(child.tag)
            if tag in ("To", "ToStep", "Branch"):
                step_ref = child.text or child.get("Ref", child.get("Step", ""))
                if step_ref:
                    to_steps.append(step_ref)

        return SFCDivergence(
            name=name,
            from_step=from_step,
            to_steps=to_steps,
        ) if to_steps else None

    @classmethod
    def _parse_convergence(cls, elem: ET.Element) -> SFCConvergence | None:
        """Parse a Convergence (parallel branch end) element."""
        name = elem.get("Name", "")
        to_step = elem.get("To", elem.get("ToStep", ""))

        from_steps = []
        for child in elem:
            tag = cls._local_tag(child.tag)
            if tag in ("From", "FromStep", "Branch"):
                step_ref = child.text or child.get("Ref", child.get("Step", ""))
                if step_ref:
                    from_steps.append(step_ref)

        return SFCConvergence(
            name=name,
            from_steps=from_steps,
            to_step=to_step,
        ) if from_steps else None

    @classmethod
    def _graph_to_st(cls, graph: SFCGraph) -> SFCConversion:
        """Convert SFC graph to ST state machine code."""
        warnings = []
        st_lines = []

        # Generate step enumeration type
        step_names = list(graph.steps.keys())
        enum_name = f"E_{graph.name}" if graph.name else "E_SFC_Step"

        st_lines.append(f"// SFC Step Enumeration")
        st_lines.append(f"TYPE {enum_name} : (")
        for i, step_name in enumerate(step_names):
            comma = "," if i < len(step_names) - 1 else ""
            initial_marker = " // Initial step" if step_name == graph.initial_step else ""
            st_lines.append(f"    {step_name}{comma}{initial_marker}")
        st_lines.append(");")
        st_lines.append("END_TYPE")
        st_lines.append("")

        # Generate state variables
        st_lines.append("// SFC State Variables")
        st_lines.append("VAR")
        st_lines.append(f"    eCurrentStep : {enum_name} := {graph.initial_step};")
        st_lines.append(f"    ePreviousStep : {enum_name};")
        st_lines.append("END_VAR")
        st_lines.append("")

        # Track stored actions (S qualifier) for reset handling
        stored_actions = set()
        for step in graph.steps.values():
            for action in step.actions:
                if action.qualifier == ActionQualifier.S:
                    stored_actions.add(action.name)

        # Generate stored action variables
        if stored_actions:
            st_lines.append("// Stored Action Flags")
            st_lines.append("VAR")
            for action_name in sorted(stored_actions):
                st_lines.append(f"    b_{action_name}_Active : BOOL := FALSE;")
            st_lines.append("END_VAR")
            st_lines.append("")

        # Generate pulse tracking variables
        pulse_actions = set()
        for step in graph.steps.values():
            for action in step.actions:
                if action.qualifier in (ActionQualifier.P, ActionQualifier.P1, ActionQualifier.P0):
                    pulse_actions.add(action.name)

        if pulse_actions:
            st_lines.append("// Pulse Action Tracking")
            st_lines.append("VAR")
            for action_name in sorted(pulse_actions):
                st_lines.append(f"    b_{action_name}_Done : BOOL := FALSE;")
            st_lines.append("END_VAR")
            st_lines.append("")

        # Generate state machine
        st_lines.append("// ========================================")
        st_lines.append("// SFC State Machine")
        st_lines.append("// ========================================")
        st_lines.append("")
        st_lines.append("ePreviousStep := eCurrentStep;")
        st_lines.append("")
        st_lines.append("CASE eCurrentStep OF")

        for step_name in step_names:
            step = graph.steps[step_name]
            st_lines.append(f"    {step_name}:")

            if step.comment:
                st_lines.append(f"        // {step.comment}")

            # Generate action code
            for action in step.actions:
                action_code = cls._generate_action_code(action, step_name)
                st_lines.append(action_code)

            # Generate transition conditions
            transitions = [t for t in graph.transitions if t.from_step == step_name]
            if transitions:
                if len(transitions) == 1:
                    trans = transitions[0]
                    condition = trans.condition or "TRUE"
                    st_lines.append(f"        // Transition: {step_name} → {trans.to_step}")
                    st_lines.append(f"        IF {condition} THEN")
                    # Reset pulse-done flags for next step
                    for action in step.actions:
                        if action.qualifier in (ActionQualifier.P, ActionQualifier.P1):
                            st_lines.append(f"            b_{action.name}_Done := FALSE;")
                    st_lines.append(f"            eCurrentStep := {trans.to_step};")
                    st_lines.append(f"        END_IF;")
                else:
                    # Multiple transitions (divergence/conditional)
                    st_lines.append(f"        // Transitions from {step_name}:")
                    for trans in transitions:
                        condition = trans.condition or "TRUE"
                        st_lines.append(f"        // → {trans.to_step}")
                        st_lines.append(f"        IF {condition} THEN")
                        st_lines.append(f"            eCurrentStep := {trans.to_step};")
                        st_lines.append(f"        END_IF;")

            st_lines.append("")

        st_lines.append("END_CASE;")
        st_lines.append("")

        # Generate stored action execution section
        if stored_actions:
            st_lines.append("// ========================================")
            st_lines.append("// Stored Action Execution")
            st_lines.append("// ========================================")
            st_lines.append("")
            for action_name in sorted(stored_actions):
                # Find the action body
                action_body = cls._find_action_body(graph, action_name)
                st_lines.append(f"IF b_{action_name}_Active THEN")
                if action_body:
                    for line in action_body.split("\n"):
                        if line.strip():
                            st_lines.append(f"    {line.strip()}")
                else:
                    st_lines.append(f"    // {action_name} action body")
                st_lines.append("END_IF;")
                st_lines.append("")

        # Handle parallel divergences
        if graph.divergences:
            st_lines.append("// ========================================")
            st_lines.append("// Parallel Branch Logic")
            st_lines.append("// ========================================")
            st_lines.append("")
            for div in graph.divergences:
                st_lines.append(f"// Parallel divergence from {div.from_step}:")
                st_lines.append(f"//   Branches to: {', '.join(div.to_steps)}")
                st_lines.append(f"// NOTE: Parallel execution requires all branches")
                st_lines.append(f"//       to complete before convergence.")
                st_lines.append("")

        if warnings:
            st_lines.append("// Warnings:")
            for w in warnings:
                st_lines.append(f"//   - {w}")

        return SFCConversion(
            st_code="\n".join(st_lines),
            step_enum_type=enum_name,
            warnings=warnings,
            conversion_notes=[
                "Converted from SFC to ST state machine (CASE statement)",
                "Step enumeration type generated for type safety",
                "Action qualifiers (N/P/S/R/L/D/P1/P0) mapped to ST logic",
                "Parallel branches annotated but require manual verification",
            ],
        )

    @classmethod
    def _generate_action_code(cls, action: SFCAction, step_name: str) -> str:
        """Generate ST code for an action based on its qualifier."""
        lines = []
        indent = "        "

        if action.qualifier == ActionQualifier.N:
            # Non-stored: active while step is active
            lines.append(f"{indent}// Action: {action.name} (N: non-stored)")
            if action.body:
                for line in action.body.split("\n"):
                    if line.strip():
                        lines.append(f"{indent}{line.strip()}")
            else:
                lines.append(f"{indent}{action.name}();")

        elif action.qualifier == ActionQualifier.P:
            # Pulse: execute once on step activation
            lines.append(f"{indent}// Action: {action.name} (P: pulse on entry)")
            lines.append(f"{indent}IF NOT b_{action.name}_Done THEN")
            if action.body:
                for line in action.body.split("\n"):
                    if line.strip():
                        lines.append(f"{indent}    {line.strip()}")
            else:
                lines.append(f"{indent}    {action.name}();")
            lines.append(f"{indent}    b_{action.name}_Done := TRUE;")
            lines.append(f"{indent}END_IF;")

        elif action.qualifier == ActionQualifier.S:
            # Set/Stored: activate and remain active
            lines.append(f"{indent}// Action: {action.name} (S: set/stored)")
            lines.append(f"{indent}b_{action.name}_Active := TRUE;")

        elif action.qualifier == ActionQualifier.R:
            # Reset: deactivate a stored action
            lines.append(f"{indent}// Action: {action.name} (R: reset)")
            lines.append(f"{indent}b_{action.name}_Active := FALSE;")

        elif action.qualifier == ActionQualifier.L:
            # Limited: active for limited time
            duration = action.duration or "T#10S"
            lines.append(f"{indent}// Action: {action.name} (L: limited to {duration})")
            lines.append(f"{indent}// Requires timer implementation")
            lines.append(f"{indent}IF ton_{action.name}(IN := TRUE, PT := {duration}) THEN")
            if action.body:
                for line in action.body.split("\n"):
                    if line.strip():
                        lines.append(f"{indent}    {line.strip()}")
            else:
                lines.append(f"{indent}    {action.name}();")
            lines.append(f"{indent}END_IF;")

        elif action.qualifier == ActionQualifier.D:
            # Delayed: activate after delay
            delay = action.delay or "T#5S"
            lines.append(f"{indent}// Action: {action.name} (D: delayed by {delay})")
            lines.append(f"{indent}// Requires timer implementation")
            lines.append(f"{indent}IF ton_{action.name}_Delay(IN := TRUE, PT := {delay}) THEN")
            if action.body:
                for line in action.body.split("\n"):
                    if line.strip():
                        lines.append(f"{indent}    {line.strip()}")
            else:
                lines.append(f"{indent}    {action.name}();")
            lines.append(f"{indent}END_IF;")

        elif action.qualifier == ActionQualifier.P1:
            # Pulse on activation
            lines.append(f"{indent}// Action: {action.name} (P1: pulse on activation)")
            lines.append(f"{indent}IF eCurrentStep <> ePreviousStep THEN")  # Step just became active
            if action.body:
                for line in action.body.split("\n"):
                    if line.strip():
                        lines.append(f"{indent}    {line.strip()}")
            else:
                lines.append(f"{indent}    {action.name}();")
            lines.append(f"{indent}END_IF;")

        elif action.qualifier == ActionQualifier.P0:
            # Pulse on deactivation
            lines.append(f"{indent}// Action: {action.name} (P0: pulse on deactivation)")
            lines.append(f"{indent}IF eCurrentStep <> {step_name} AND ePreviousStep = {step_name} THEN")
            if action.body:
                for line in action.body.split("\n"):
                    if line.strip():
                        lines.append(f"{indent}    {line.strip()}")
            else:
                lines.append(f"{indent}    {action.name}();")
            lines.append(f"{indent}END_IF;")

        return "\n".join(lines)

    @classmethod
    def _find_action_body(cls, graph: SFCGraph, action_name: str) -> str | None:
        """Find the body of a named action across all steps."""
        for step in graph.steps.values():
            for action in step.actions:
                if action.name == action_name and action.body:
                    return action.body
        return None

    @classmethod
    def _extract_cdata(cls, elem: ET.Element) -> str:
        """Extract text content (handles CDATA)."""
        text = elem.text or ""
        for sub in elem:
            sub_text = sub.text or ""
            if sub_text:
                text += sub_text
        return text.strip()

    @classmethod
    def _local_tag(cls, tag: str) -> str:
        """Extract local tag name from namespaced tag."""
        if "}" in tag:
            return tag.split("}")[1]
        return tag

    @classmethod
    def has_sfc_marker(cls, source_code: str) -> bool:
        """Check if source code contains SFC XML markers."""
        return bool(re.search(r"\[SFC_XML:", source_code))

    @classmethod
    def extract_and_convert(cls, source_code: str) -> SFCConversion:
        """Extract SFC XML from marked source and convert to ST."""
        pattern = r"\[SFC_XML:(.*?)\]"
        matches = list(re.finditer(pattern, source_code, re.DOTALL))

        if not matches:
            return SFCConversion(st_code=source_code)

        all_st = []
        all_warnings = []

        for match in matches:
            xml_content = match.group(1)
            result = cls.convert_xml_to_st(xml_content)
            if result.st_code:
                all_st.append("// [SFC → ST State Machine Conversion]")
                all_st.append(result.st_code)
            all_warnings.extend(result.warnings)

        return SFCConversion(
            st_code="\n\n".join(all_st),
            warnings=all_warnings,
            conversion_notes=[
                f"Converted {len(matches)} SFC network(s) to ST state machine",
            ],
        )
