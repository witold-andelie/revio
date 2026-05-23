"""Rockwell/Allen-Bradley Studio 5000 L5X parser.

Parses L5X XML export files from Studio 5000 (formerly RSLogix 5000).
L5X is the standard interchange format for ControlLogix and CompactLogix
controllers widely used in European manufacturing (automotive, pharma, F&B).

Structure of an L5X file:
  <RSLogix5000Content>
    <Controller Name="MyController" ...>
      <Tags>
        <Tag Name="g_Start" DataType="BOOL" Scope="Controller" .../>
      </Tags>
      <DataTypes>
        <DataType Name="MyUDT" Family="NoFamily" Class="User">
          <Members>
            <Member Name="Field1" DataType="BOOL" Dimension="0" .../>
          </Members>
        </DataType>
      </DataTypes>
      <Programs>
        <Program Name="MainProgram" ...>
          <Tags>
            <Tag Name="i_Start" DataType="BOOL" Scope="Program" .../>
          </Tags>
          <Routines>
            <Routine Name="MainRoutine" Type="ST">
              <STContent>
                <![CDATA[ <ST code> ]]>
              </STContent>
            </Routine>
            <Routine Name="LadderRoutine" Type="RLL">
              <RLLContent>
                <Rung Number="0" Type="N">
                  <Text><![CDATA[ XIC(i_Start) OTE(o_Motor); ]]></Text>
                  <Comment><![CDATA[ Start/Stop logic ]]></Comment>
                </Rung>
              </RLLContent>
            </Routine>
          </Routines>
        </Program>
      </Programs>
    </Controller>
  </RSLogix5000Content>
"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class RockwellTag(BaseModel):
    """A tag (variable) in a Rockwell controller."""
    name: str
    data_type: str
    scope: str  # Controller, Program
    description: str | None = None
    default_value: str | None = None
    dimensions: list[int] = []  # Array dimensions
    external_access: str = "Read/Write"


class RockwellDataType(BaseModel):
    """A user-defined data type (UDT)."""
    name: str
    family: str = "NoFamily"
    class_type: str = "User"
    members: list[dict] = []


class RockwellRung(BaseModel):
    """A single ladder rung."""
    number: int
    rung_type: str = "N"  # N=Normal, E=Empty, U=User
    text: str = ""  # The instruction text (XIC, OTE, etc.)
    comment: str | None = None


class RockwellRoutine(BaseModel):
    """A routine in a program."""
    name: str
    routine_type: str  # ST, RLL, FBD, SFC
    st_code: str = ""  # Structured text content
    rungs: list[RockwellRung] = []  # Ladder rungs
    raw_xml: str = ""  # For graphical types


class RockwellProgram(BaseModel):
    """A program in the controller."""
    name: str
    tags: list[RockwellTag] = []
    routines: list[RockwellRoutine] = []
    description: str | None = None
    scheduled: bool = True


class RockwellController(BaseModel):
    """The controller (PLC) configuration."""
    name: str
    controller_type: str = ""  # e.g., "1756-L83ES"
    major_rev: int = 0
    minor_rev: int = 0
    tags: list[RockwellTag] = []
    data_types: list[RockwellDataType] = []
    programs: list[RockwellProgram] = []


class RockwellProject(BaseModel):
    """A parsed Rockwell L5X project."""
    schema_version: str = ""
    controller: RockwellController | None = None
    file_path: str = ""

    @property
    def all_routines(self) -> list[RockwellRoutine]:
        """Get all routines from all programs."""
        if not self.controller:
            return []
        return [r for prog in self.controller.programs for r in prog.routines]

    @property
    def all_tags(self) -> list[RockwellTag]:
        """Get all tags (controller + program scoped)."""
        if not self.controller:
            return []
        tags = list(self.controller.tags)
        for prog in self.controller.programs:
            tags.extend(prog.tags)
        return tags


class RockwellParser:
    """Parse Rockwell/Allen-Bradley Studio 5000 L5X XML files."""

    @classmethod
    def parse_file(cls, file_path: str) -> RockwellProject | None:
        """Parse an L5X file."""
        path = Path(file_path)
        if not path.exists():
            return None

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f"XML parse error in {file_path}: {e}")
            return None

        return cls._parse_root(root, file_path)

    @classmethod
    def _parse_root(cls, root: ET.Element, file_path: str) -> RockwellProject | None:
        """Parse the RSLogix5000Content root element."""
        if root.tag != "RSLogix5000Content":
            # Try without namespace
            if not root.tag.endswith("RSLogix5000Content"):
                return None

        project = RockwellProject(
            schema_version=root.get("SchemaRevision", ""),
            file_path=file_path,
        )

        # Find Controller element
        controller_elem = root.find("Controller")
        if controller_elem is None:
            return None

        project.controller = cls._parse_controller(controller_elem)
        return project if project.controller else None

    @classmethod
    def _parse_controller(cls, elem: ET.Element) -> RockwellController | None:
        """Parse a Controller element."""
        controller = RockwellController(
            name=elem.get("Name", "Unknown"),
            controller_type=elem.get("Type", ""),
            major_rev=int(elem.get("MajorRev", "0")),
            minor_rev=int(elem.get("MinorRev", "0")),
        )

        # Parse controller-scoped tags
        tags_elem = elem.find("Tags")
        if tags_elem is not None:
            controller.tags = cls._parse_tags(tags_elem, "Controller")

        # Parse data types
        dt_elem = elem.find("DataTypes")
        if dt_elem is not None:
            controller.data_types = cls._parse_data_types(dt_elem)

        # Parse programs
        programs_elem = elem.find("Programs")
        if programs_elem is not None:
            for prog_elem in programs_elem.findall("Program"):
                prog = cls._parse_program(prog_elem)
                if prog:
                    controller.programs.append(prog)

        return controller

    @classmethod
    def _parse_tags(cls, tags_elem: ET.Element, scope: str) -> list[RockwellTag]:
        """Parse Tags section."""
        tags = []
        for tag_elem in tags_elem.findall("Tag"):
            name = tag_elem.get("Name", "")
            if not name:
                continue

            # Skip internal/system tags
            if name.startswith("__"):
                continue

            # Get dimensions
            dims = []
            dim_str = tag_elem.get("Dimensions", "")
            if dim_str:
                try:
                    dims = [int(d) for d in dim_str.split(",") if d.strip()]
                except ValueError:
                    pass

            tags.append(RockwellTag(
                name=name,
                data_type=tag_elem.get("DataType", "UNKNOWN"),
                scope=scope,
                description=cls._get_description(tag_elem),
                default_value=tag_elem.get("DefaultVisibleValue"),
                dimensions=dims,
                external_access=tag_elem.get("ExternalAccess", "Read/Write"),
            ))

        return tags

    @classmethod
    def _parse_data_types(cls, dt_elem: ET.Element) -> list[RockwellDataType]:
        """Parse DataTypes section."""
        data_types = []
        for dt in dt_elem.findall("DataType"):
            members = []
            members_elem = dt.find("Members")
            if members_elem is not None:
                for member in members_elem.findall("Member"):
                    members.append({
                        "name": member.get("Name", ""),
                        "data_type": member.get("DataType", ""),
                        "dimension": member.get("Dimensions", "0"),
                        "description": cls._get_description(member),
                    })

            data_types.append(RockwellDataType(
                name=dt.get("Name", "Unknown"),
                family=dt.get("Family", "NoFamily"),
                class_type=dt.get("Class", "User"),
                members=members,
            ))

        return data_types

    @classmethod
    def _parse_program(cls, prog_elem: ET.Element) -> RockwellProgram | None:
        """Parse a Program element."""
        program = RockwellProgram(
            name=prog_elem.get("Name", "Unknown"),
            description=cls._get_description(prog_elem),
            scheduled=prog_elem.get("Scheduled", "true").lower() == "true",
        )

        # Parse program-scoped tags
        tags_elem = prog_elem.find("Tags")
        if tags_elem is not None:
            program.tags = cls._parse_tags(tags_elem, "Program")

        # Parse routines
        routines_elem = prog_elem.find("Routines")
        if routines_elem is not None:
            for routine_elem in routines_elem.findall("Routine"):
                routine = cls._parse_routine(routine_elem)
                if routine:
                    program.routines.append(routine)

        return program

    @classmethod
    def _parse_routine(cls, routine_elem: ET.Element) -> RockwellRoutine | None:
        """Parse a Routine element."""
        name = routine_elem.get("Name", "Unknown")
        routine_type = routine_elem.get("Type", "ST")

        routine = RockwellRoutine(
            name=name,
            routine_type=routine_type,
        )

        if routine_type == "ST":
            # Structured Text routine
            st_elem = routine_elem.find("STContent")
            if st_elem is not None:
                routine.st_code = cls._extract_cdata_text(st_elem)

        elif routine_type == "RLL":
            # Ladder logic routine
            rll_elem = routine_elem.find("RLLContent")
            if rll_elem is not None:
                routine.rungs = cls._parse_rungs(rll_elem)

        else:
            # FBD, SFC, or other graphical type
            routine.raw_xml = ET.tostring(routine_elem, encoding="unicode")

        return routine

    @classmethod
    def _parse_rungs(cls, rll_elem: ET.Element) -> list[RockwellRung]:
        """Parse RLLContent into rungs."""
        rungs = []
        for rung_elem in rll_elem.findall("Rung"):
            number = int(rung_elem.get("Number", "0"))
            rung_type = rung_elem.get("Type", "N")

            # Extract instruction text
            text_elem = rung_elem.find("Text")
            text = cls._extract_cdata_text(text_elem) if text_elem is not None else ""

            # Extract comment
            comment_elem = rung_elem.find("Comment")
            comment = cls._extract_cdata_text(comment_elem) if comment_elem is not None else None

            rungs.append(RockwellRung(
                number=number,
                rung_type=rung_type,
                text=text,
                comment=comment,
            ))

        return rungs

    @classmethod
    def _extract_cdata_text(cls, elem: ET.Element) -> str:
        """Extract text content (handles CDATA)."""
        text = elem.text or ""
        for sub in elem:
            sub_text = sub.text or ""
            if sub_text:
                text += sub_text
        return text.strip()

    @classmethod
    def _get_description(cls, elem: ET.Element) -> str | None:
        """Extract Description element text."""
        desc_elem = elem.find("Description")
        if desc_elem is not None:
            return cls._extract_cdata_text(desc_elem)
        return None

    @classmethod
    def extract_st_from_file(cls, file_path: str) -> str | None:
        """Quick helper: extract all ST code from an L5X file."""
        project = cls.parse_file(file_path)
        if not project:
            return None

        parts = []
        for routine in project.all_routines:
            if routine.routine_type == "ST" and routine.st_code:
                parts.append(f"// Routine: {routine.name}")
                parts.append(routine.st_code)
            elif routine.routine_type == "RLL" and routine.rungs:
                # Convert ladder rungs to pseudo-ST for LLM analysis
                parts.append(f"// Routine: {routine.name} (Ladder)")
                parts.append(cls._rungs_to_pseudo_st(routine.rungs))

        return "\n\n".join(parts) if parts else None

    @classmethod
    def _rungs_to_pseudo_st(cls, rungs: list[RockwellRung]) -> str:
        """Convert ladder rungs to pseudo-ST for LLM analysis.

        Rockwell ladder instructions use a different syntax than IEC 61131-3 LD:
        - XIC(tag) = Examine If Closed (NO contact)
        - XIO(tag) = Examine If Open (NC contact)
        - OTE(tag) = Output Energize (coil)
        - OTL(tag) = Output Latch (set)
        - OTU(tag) = Output Unlatch (reset)
        - TON(timer, preset) = Timer On Delay
        - CTU(counter, preset) = Count Up
        """
        st_lines = []

        for rung in rungs:
            if not rung.text.strip():
                continue

            if rung.comment:
                st_lines.append(f"// {rung.comment}")

            # Parse the rung instruction text
            st_code = cls._convert_rung_to_st(rung.text)
            st_lines.append(st_code)

        return "\n".join(st_lines)

    @classmethod
    def _convert_rung_to_st(cls, rung_text: str) -> str:
        """Convert a single Rockwell ladder rung to ST equivalent.

        Handles:
        - XIC(tag) → tag
        - XIO(tag) → NOT tag
        - OTE(tag) → tag := result
        - OTL(tag) → tag := TRUE (latch)
        - OTU(tag) → tag := FALSE (unlatch)
        - TON(timer, preset, acc) → timer(...)
        - Series instructions → AND
        - Parallel branches → OR
        """
        # Extract all instructions
        instructions = re.findall(r"(\w+)\(([^)]*)\)", rung_text)

        conditions = []
        outputs = []
        fbs = []

        for opcode, operands in instructions:
            opcode = opcode.upper()

            if opcode == "XIC":
                # Examine If Closed (NO contact)
                conditions.append(operands.strip())
            elif opcode == "XIO":
                # Examine If Open (NC contact)
                conditions.append(f"NOT {operands.strip()}")
            elif opcode in ("OTE", "OTL", "OTU"):
                # Output instructions
                tag = operands.strip()
                if opcode == "OTE":
                    outputs.append(f"{tag} := {{result}};")
                elif opcode == "OTL":
                    outputs.append(f"{tag} := TRUE; // Latch")
                elif opcode == "OTU":
                    outputs.append(f"{tag} := FALSE; // Unlatch")
            elif opcode in ("TON", "TOF", "TP", "CTU", "CTD", "RTO"):
                # Function block calls
                fbs.append(f"{opcode}({operands});")

        # Build ST
        st_parts = []

        # Function blocks first
        for fb in fbs:
            st_parts.append(fb)

        # Conditional logic
        if conditions and outputs:
            condition_str = " AND ".join(conditions)
            st_parts.append(f"IF {condition_str} THEN")
            for out in outputs:
                st_parts.append(f"    {out.replace('{result}', 'TRUE')}")
            st_parts.append("ELSE")
            for out in outputs:
                if "Latch" not in out and "Unlatch" not in out:
                    st_parts.append(f"    {out.replace('{result}', 'FALSE')}")
            st_parts.append("END_IF;")
        elif outputs:
            # Unconditional output
            for out in outputs:
                st_parts.append(out.replace("{result}", "TRUE"))

        return "\n".join(st_parts) if st_parts else f"// {rung_text}"

    @classmethod
    def is_l5x(cls, file_path: str) -> bool:
        """Check if a file is a Rockwell L5X file."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            if root.tag == "RSLogix5000Content":
                return True
            # Check for controller element
            if root.find("Controller") is not None:
                return True
            return False
        except ET.ParseError:
            return False
