"""Beckhoff TwinCAT 3 TcPOU XML parser.

Parses TwinCAT POU (Program Organization Unit) files that use CDATA blocks
to separate Structured Text code from XML markup.

Structure of a typical TwinCAT TcPOU file:
  <TcPlcObject Version="1.1.0.1">
    <POU Name="FB_Motor" Id="{GUID}">
      <POUType>FunctionBlock</POUType>
      <Declaration><![CDATA[
        FUNCTION_BLOCK FB_Motor
        VAR_INPUT
          i_Start : BOOL;
        END_VAR
        ...
      ]]></Declaration>
      <Implementation>
        <ST><![CDATA[
          <SCL code here>
        ]]></ST>
      </Implementation>
    </POU>
    <GlobalVariables>
      <Variable Name="g_Start" ...>
        <Declaration><![CDATA[VAR_GLOBAL ... END_VAR]]></Declaration>
      </Variable>
    </GlobalVariables>
  </TcPlcObject>
"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class TwincatVariable(BaseModel):
    """A variable declared in a TwinCAT POU."""
    name: str
    datatype: str
    scope: str  # VAR_INPUT, VAR_OUTPUT, VAR, VAR_IN_OUT, VAR_GLOBAL, VAR_EXTERNAL
    address: str | None = None  # AT %I0.0, AT %Q0.1
    initial_value: str | None = None
    comment: str | None = None


class TwincatPOU(BaseModel):
    """A parsed TwinCAT Program Organization Unit."""
    name: str
    pou_type: str  # Program, FunctionBlock, Function
    declaration: str  # Raw declaration text from CDATA
    implementation: str  # Raw ST implementation from CDATA
    variables: list[TwincatVariable] = []
    file_path: str = ""
    guid: str | None = None


class TwincatProject(BaseModel):
    """A parsed TwinCAT project (may contain multiple POUs)."""
    version: str | None = None
    pous: list[TwincatPOU] = []
    global_variables: list[TwincatVariable] = []
    file_path: str = ""


class TwincatParser:
    """Parse Beckhoff TwinCAT 3 TcPOU XML files with CDATA handling."""

    @classmethod
    def parse_file(cls, file_path: str) -> TwincatProject | None:
        """Parse a TwinCAT XML file and extract all POUs."""
        path = Path(file_path)
        if not path.exists():
            return None

        try:
            # Parse with CDATA support (default ElementTree preserves CDATA text)
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f"XML parse error in {file_path}: {e}")
            return None

        return cls._parse_root(root, file_path)

    @classmethod
    def _parse_root(cls, root: ET.Element, file_path: str) -> TwincatProject | None:
        """Parse the TcPlcObject root element."""
        version = root.get("Version")

        project = TwincatProject(
            version=version,
            file_path=file_path,
        )

        # Parse POUs
        for pou_elem in root.iter():
            if pou_elem.tag == "POU" or pou_elem.tag.endswith("}POU"):
                pou = cls._parse_pou(pou_elem, file_path)
                if pou:
                    project.pous.append(pou)

        # Parse global variables
        for gv_elem in root.iter():
            if gv_elem.tag == "GlobalVariables" or gv_elem.tag.endswith("}GlobalVariables"):
                for var_elem in gv_elem.iter():
                    if var_elem.tag == "Variable" or var_elem.tag.endswith("}Variable"):
                        var = cls._parse_variable_element(var_elem, "VAR_GLOBAL")
                        if var:
                            project.global_variables.append(var)

        return project if project.pous or project.global_variables else None

    @classmethod
    def _parse_pou(cls, pou_elem: ET.Element, file_path: str) -> TwincatPOU | None:
        """Parse a single POU element."""
        name = pou_elem.get("Name", "Unknown")
        guid = pou_elem.get("Id")

        # Determine POU type
        pou_type = "FunctionBlock"  # default
        for child in pou_elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "POUType" and child.text:
                pou_type = child.text.strip()
                break

        # Extract Declaration (CDATA)
        declaration = cls._extract_cdata_text(pou_elem, "Declaration")

        # Extract Implementation → ST (CDATA)
        implementation = cls._extract_implementation(pou_elem)

        # Parse variables from declaration
        variables = cls._parse_declaration_variables(declaration)

        return TwincatPOU(
            name=name,
            pou_type=pou_type,
            declaration=declaration,
            implementation=implementation,
            variables=variables,
            file_path=file_path,
            guid=guid,
        )

    @classmethod
    def _extract_cdata_text(cls, parent: ET.Element, tag_name: str) -> str:
        """Extract text content from an element, including CDATA content."""
        for child in parent:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == tag_name:
                # .text on ElementTree preserves CDATA content
                text = child.text or ""
                # Also check for nested text (CDATA is in .text)
                for sub in child:
                    sub_text = sub.text or ""
                    if sub_text:
                        text += sub_text
                return text.strip()
        return ""

    @classmethod
    def _extract_implementation(cls, pou_elem: ET.Element) -> str:
        """Extract ST implementation code from Implementation/ST/CDATA."""
        for child in pou_elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "Implementation":
                # Look for ST element (could also be LD, FBD, IL, SFC)
                for st_elem in child:
                    st_tag = st_elem.tag.split("}")[-1] if "}" in st_elem.tag else st_elem.tag
                    if st_tag == "ST":
                        return (st_elem.text or "").strip()
                    elif st_tag == "LD":
                        # Ladder Diagram - return marker for conversion
                        xml_str = ET.tostring(st_elem, encoding="unicode")
                        return f"[LD_XML:{xml_str}]"
                    elif st_tag == "FBD":
                        xml_str = ET.tostring(st_elem, encoding="unicode")
                        return f"[FBD_XML:{xml_str}]"
                    elif st_tag == "IL":
                        return (st_elem.text or "").strip()
                    elif st_tag == "SFC":
                        xml_str = ET.tostring(st_elem, encoding="unicode")
                        return f"[SFC_XML:{xml_str}]"

                # If no specific language element, get all text
                text = ET.tostring(child, encoding="unicode", method="text")
                if text and text.strip():
                    return text.strip()

        return ""

    @classmethod
    def _parse_declaration_variables(cls, declaration: str) -> list[TwincatVariable]:
        """Parse variable declarations from the CDATA declaration text."""
        if not declaration:
            return []

        variables = []
        current_scope = "VAR"

        # Split into lines and process
        lines = declaration.split("\n")

        for line in lines:
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith("//") or stripped.startswith("(*"):
                continue

            # Detect scope block
            scope_upper = stripped.upper()
            if scope_upper.startswith("VAR_INPUT"):
                current_scope = "VAR_INPUT"
                continue
            elif scope_upper.startswith("VAR_OUTPUT"):
                current_scope = "VAR_OUTPUT"
                continue
            elif scope_upper.startswith("VAR_IN_OUT"):
                current_scope = "VAR_IN_OUT"
                continue
            elif scope_upper.startswith("VAR_GLOBAL"):
                current_scope = "VAR_GLOBAL"
                continue
            elif scope_upper.startswith("VAR_EXTERNAL"):
                current_scope = "VAR_EXTERNAL"
                continue
            elif scope_upper.startswith("VAR_TEMP"):
                current_scope = "VAR_TEMP"
                continue
            elif scope_upper == "VAR" or scope_upper.startswith("VAR "):
                current_scope = "VAR"
                # Check if declaration is on same line as VAR keyword
                match = re.match(r"VAR\s+(\w+)\s*:\s*(\w+.*)", stripped, re.IGNORECASE)
                if match:
                    name = match.group(1)
                    rest = match.group(2)
                    variables.append(cls._parse_var_line(name, rest, current_scope))
                continue
            elif scope_upper == "END_VAR":
                current_scope = "VAR"
                continue

            # Skip function block name line (FUNCTION_BLOCK, FUNCTION, PROGRAM)
            if re.match(r"(FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+\w+", stripped, re.IGNORECASE):
                continue

            # Skip RETURN type
            if scope_upper.startswith("RETURN"):
                continue

            # Parse variable declaration line: name : datatype [:= value] [// comment]
            match = re.match(r"(\w+)\s*:\s*(.+?)(?:\s*:=\s*(.+?))?(?:\s*//.*)?$", stripped)
            if match:
                name = match.group(1)
                rest = match.group(2).strip()
                initial = match.group(3)
                if initial:
                    initial = initial.strip().rstrip(";").strip()

                variables.append(cls._parse_var_line(name, rest, current_scope, initial))

        return variables

    @classmethod
    def _parse_var_line(
        cls,
        name: str,
        datatype_part: str,
        scope: str,
        initial_value: str | None = None,
    ) -> TwincatVariable:
        """Parse a single variable declaration line."""
        # Extract comment
        comment = None
        comment_match = re.search(r"//(.+)$", datatype_part)
        if comment_match:
            comment = comment_match.group(1).strip()
            datatype_part = datatype_part[:comment_match.start()].strip()

        # Extract AT address
        address = None
        at_match = re.search(r"AT\s+(%[A-Z]\d+\.\d+)", datatype_part, re.IGNORECASE)
        if at_match:
            address = at_match.group(1)
            datatype_part = datatype_part[:at_match.start()].strip() + \
                           datatype_part[at_match.end():].strip()

        # Clean up datatype (remove trailing semicolons, etc.)
        datatype = datatype_part.strip().rstrip(";").strip()

        # Extract initial value from datatype if present
        if not initial_value:
            init_match = re.search(r":=\s*(.+?)(?:\s*;|\s*$)", datatype_part)
            if init_match:
                initial_value = init_match.group(1).strip()
                datatype = datatype[:init_match.start()].strip()

        return TwincatVariable(
            name=name,
            datatype=datatype,
            scope=scope,
            address=address,
            initial_value=initial_value,
            comment=comment,
        )

    @classmethod
    def extract_st_from_file(cls, file_path: str) -> str | None:
        """Quick helper: extract all ST implementation code from a TcPOU file."""
        project = cls.parse_file(file_path)
        if not project:
            return None

        parts = []
        for pou in project.pous:
            if pou.implementation and not pou.implementation.startswith("["):
                parts.append(f"// {pou.pou_type} {pou.name}")
                parts.append(pou.implementation)

        return "\n\n".join(parts) if parts else None

    @classmethod
    def extract_all_st_blocks(cls, file_path: str) -> list[tuple[str, str, str]]:
        """Extract (pou_name, pou_type, st_code) tuples from a TcPOU file."""
        project = cls.parse_file(file_path)
        if not project:
            return []

        blocks = []
        for pou in project.pous:
            if pou.implementation and not pou.implementation.startswith("["):
                blocks.append((pou.name, pou.pou_type, pou.implementation))
        return blocks

    @classmethod
    def is_twincat(cls, file_path: str) -> bool:
        """Check if a file is a TwinCAT TcPOU XML file."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            if root.tag == "TcPlcObject":
                return True
            # Check for TwinCAT-specific elements
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag in ("TcPOU", "TcGVL", "TcDUT"):
                    return True
            return False
        except ET.ParseError:
            return False
