"""Siemens TIA Portal SimaticML XML parser.

Parses deeply nested SimaticML XML exported from TIA Portal (S7-1200/S7-1500).
Extracts SCL code from CodeBlock/ObjectList regions while filtering out
version-specific metadata and Base64-encoded hardware configurations.

Structure of a typical SimaticML export:
  <Document>
    <Engineering version="V17" />
    <SW.Blocks.FC Name="FC_Name">        (or FB, OB, DB)
      <Interface>
        <Sections>
          <Section Name="Input">
            <Member Name="..." Datatype="BOOL" />
          </Section>
          <Section Name="Output"> ... </Section>
          <Section Name="Static"> ... </Section>
          <Section Name="Temp">   ... </Section>
          <Section Name="InOut">  ... </Section>
        </Sections>
      </Interface>
      <ObjectList>
        <SW.Blocks.CompileUnit ID="0">
          <ProgrammingLanguage>SCL</ProgrammingLanguage>
          <CodeBlock>
            <Network SourceCode="...SCL code..." />
          </CodeBlock>
        </SW.Blocks.CompileUnit>
      </ObjectList>
    </SW.Blocks.FC>
  </Document>
"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# TIA Portal XML namespaces (varies by version)
SIMATIC_NS = {
    "http://www.siemens.com/automation/Openness/SW/Interface/v1": "swi",
    "http://www.siemens.com/automation/Openness/SW/NetworkSource/ConsistentClasses/v1": "swn",
    "": "",
}


class SimaticVariable(BaseModel):
    """A variable declared in the PLC interface."""
    name: str
    datatype: str
    scope: str  # Input, Output, Static, Temp, InOut, Constant
    start_value: str | None = None
    comment: str | None = None
    read_only: bool = False
    address: str | None = None  # e.g. "%I0.0", "%Q0.1"


class SimaticNetwork(BaseModel):
    """A single network (rung) in the SCL code."""
    number: int = 0
    title: str | None = None
    source_code: str = ""
    comment: str | None = None


class SimaticBlock(BaseModel):
    """A parsed TIA Portal program block."""
    name: str
    block_type: str  # FC, FB, OB, DB
    programming_language: str  # SCL, LAD, FBD, STL, S7-GRAPH
    networks: list[SimaticNetwork] = []
    variables: list[SimaticVariable] = []
    source_code: str = ""  # Combined SCL code from all networks
    file_path: str = ""
    tia_version: str | None = None


class SimaticMLParser:
    """Parse Siemens TIA Portal SimaticML XML files."""

    # Map SW.Blocks.* to block type
    BLOCK_TYPE_MAP = {
        "SW.Blocks.FC": "FC",
        "SW.Blocks.FB": "FB",
        "SW.Blocks.OB": "OB",
        "SW.Blocks.DB": "DB",
        "SW.Blocks.GlobalDB": "DB",
        "SW.Blocks.SystemDB": "DB",
    }

    @classmethod
    def parse_file(cls, file_path: str) -> SimaticBlock | None:
        """Parse a SimaticML XML file."""
        path = Path(file_path)
        if not path.exists():
            return None

        try:
            # Use iterparse to handle large XML files efficiently
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f"XML parse error in {file_path}: {e}")
            return None

        return cls._parse_document(root, file_path)

    @classmethod
    def _parse_document(cls, root: ET.Element, file_path: str) -> SimaticBlock | None:
        """Parse the Document root element."""
        # Extract TIA version from Engineering element
        tia_version = None
        engineering = root.find(".//Engineering")
        if engineering is not None:
            tia_version = engineering.get("version")

        # Find the block element (SW.Blocks.FC, SW.Blocks.FB, etc.)
        block_elem = None
        block_type = None
        for tag, btype in cls.BLOCK_TYPE_MAP.items():
            # Try with and without namespace
            elem = root.find(f".//{{{tag}}}") if "}" in tag else root.find(f".//{tag}")
            if elem is None:
                elem = root.find(f".//{tag}")
            if elem is not None:
                block_elem = elem
                block_type = btype
                break

        if block_elem is None:
            # Try generic approach: look for any element with BlockType attribute
            for elem in root.iter():
                if elem.get("BlockType"):
                    block_elem = elem
                    block_type = elem.get("BlockType")
                    break

        if block_elem is None:
            logger.warning(f"No block element found in {file_path}")
            return None

        block_name = block_elem.get("Name", "Unknown")

        # Determine programming language
        prog_lang = cls._detect_programming_language(block_elem)

        # Extract interface variables
        variables = cls._extract_interface(block_elem)

        # Extract code networks
        networks = cls._extract_networks(block_elem, prog_lang)

        # Combine all network source code
        combined_code = "\n\n".join(
            n.source_code for n in networks if n.source_code
        )

        return SimaticBlock(
            name=block_name,
            block_type=block_type,
            programming_language=prog_lang,
            networks=networks,
            variables=variables,
            source_code=combined_code,
            file_path=file_path,
            tia_version=tia_version,
        )

    @classmethod
    def _detect_programming_language(cls, block_elem: ET.Element) -> str:
        """Detect the programming language of the block."""
        # Check ProgrammingLanguage element
        for elem in block_elem.iter():
            if elem.tag.endswith("ProgrammingLanguage") or elem.tag == "ProgrammingLanguage":
                if elem.text:
                    return elem.text.strip().upper()

        # Check for SCL-specific patterns in code
        for elem in block_elem.iter():
            if elem.tag.endswith("CodeBlock") or elem.tag == "CodeBlock":
                text = ET.tostring(elem, encoding="unicode", method="text")
                if re.search(r"\bIF\b.*\bTHEN\b", text, re.IGNORECASE):
                    return "SCL"

        return "SCL"  # Default assumption

    @classmethod
    def _extract_interface(cls, block_elem: ET.Element) -> list[SimaticVariable]:
        """Extract variable declarations from the Interface section."""
        variables = []

        # Find Interface element
        interface = None
        for elem in block_elem.iter():
            if elem.tag.endswith("Interface") or elem.tag == "Interface":
                interface = elem
                break

        if interface is None:
            return variables

        # Iterate through Sections
        for section in interface.iter():
            if not (section.tag.endswith("Section") or section.tag == "Section"):
                continue

            scope = section.get("Name", "Unknown")

            # Extract Members
            for member in section.iter():
                if not (member.tag.endswith("Member") or member.tag == "Member"):
                    continue

                name = member.get("Name", "")
                if not name:
                    continue

                datatype = member.get("Datatype", "UNKNOWN")
                start_value = member.get("StartValue")
                read_only = member.get("ReadOnly", "FALSE").upper() == "TRUE"
                address = member.get("Address")

                # Extract comment if present
                comment = None
                for child in member:
                    if child.tag.endswith("Comment") or child.tag == "Comment":
                        comment = child.text
                        break

                variables.append(SimaticVariable(
                    name=name,
                    datatype=datatype,
                    scope=scope,
                    start_value=start_value,
                    comment=comment,
                    read_only=read_only,
                    address=address,
                ))

        return variables

    @classmethod
    def _extract_networks(
        cls, block_elem: ET.Element, prog_lang: str
    ) -> list[SimaticNetwork]:
        """Extract code networks from ObjectList → CompileUnit → CodeBlock."""
        networks = []

        # Navigate: ObjectList → CompileUnit
        for obj_list in block_elem.iter():
            if not (obj_list.tag.endswith("ObjectList") or obj_list.tag == "ObjectList"):
                continue

            for compile_unit in obj_list.iter():
                if not (compile_unit.tag.endswith("CompileUnit") or
                        compile_unit.tag == "CompileUnit"):
                    continue

                network = cls._parse_compile_unit(compile_unit, prog_lang)
                if network and network.source_code:
                    networks.append(network)

        # If no networks found, try direct CodeBlock extraction
        if not networks:
            combined = cls._extract_code_from_codeblocks(block_elem)
            if combined:
                networks.append(SimaticNetwork(
                    number=0,
                    source_code=combined,
                ))

        # If still nothing and language is LAD/FBD, flag for LD conversion
        if not networks and prog_lang in ("LAD", "FBD"):
            networks.append(SimaticNetwork(
                number=0,
                source_code=f"[GRAPHICAL_LANGUAGE:{prog_lang}]",
                comment="Block uses graphical programming language - requires LD-to-ST conversion",
            ))

        return networks

    @classmethod
    def _parse_compile_unit(
        cls, compile_unit: ET.Element, prog_lang: str
    ) -> SimaticNetwork | None:
        """Parse a single CompileUnit into a SimaticNetwork."""
        network = SimaticNetwork()

        # Get network number
        network.number = int(compile_unit.get("ID", "0"))

        # Get network title
        for elem in compile_unit.iter():
            if elem.tag.endswith("Title") or elem.tag == "Title":
                network.title = elem.text
                break

        # Get network comment
        for elem in compile_unit.iter():
            if elem.tag.endswith("Comment") or elem.tag == "Comment":
                network.comment = elem.text
                break

        # Extract source code depending on language
        if prog_lang == "SCL":
            network.source_code = cls._extract_scl_code(compile_unit)
        elif prog_lang in ("LAD", "FBD"):
            network.source_code = cls._extract_ld_xml(compile_unit)
        else:
            # STL or other text-based language
            network.source_code = cls._extract_text_code(compile_unit)

        return network

    @classmethod
    def _extract_scl_code(cls, compile_unit: ET.Element) -> str:
        """Extract SCL source code from a CompileUnit."""
        code_parts = []

        # Method 1: SourceCode attribute on Network element
        for elem in compile_unit.iter():
            if elem.tag.endswith("Network") or elem.tag == "Network":
                src = elem.get("SourceCode")
                if src:
                    code_parts.append(src)

        # Method 2: Text content of CodeBlock
        for elem in compile_unit.iter():
            if elem.tag.endswith("CodeBlock") or elem.tag == "CodeBlock":
                text = ET.tostring(elem, encoding="unicode", method="text")
                if text and text.strip():
                    code_parts.append(text.strip())

        # Method 3: FlattenedSourceCode
        for elem in compile_unit.iter():
            if elem.tag.endswith("FlattenedSourceCode") or elem.tag == "FlattenedSourceCode":
                if elem.text:
                    code_parts.append(elem.text.strip())

        return "\n".join(code_parts)

    @classmethod
    def _extract_ld_xml(cls, compile_unit: ET.Element) -> str:
        """Extract LD/FBD XML structure for later conversion to ST."""
        # Return the raw XML of the network for LD-to-ST conversion
        xml_str = ET.tostring(compile_unit, encoding="unicode")
        # Strip namespace prefixes for cleaner processing
        xml_str = re.sub(r'\sxmlns[^"]*"[^"]*"', '', xml_str)
        return f"[LD_XML:{xml_str}]"

    @classmethod
    def _extract_text_code(cls, compile_unit: ET.Element) -> str:
        """Extract text-based code (STL, etc.)."""
        code_parts = []
        for elem in compile_unit.iter():
            if elem.tag.endswith("SourceCode") or elem.tag == "SourceCode":
                if elem.text:
                    code_parts.append(elem.text.strip())
        return "\n".join(code_parts)

    @classmethod
    def _extract_code_from_codeblocks(cls, block_elem: ET.Element) -> str:
        """Fallback: extract code from any CodeBlock element."""
        parts = []
        for elem in block_elem.iter():
            if elem.tag.endswith("CodeBlock") or elem.tag == "CodeBlock":
                text = ET.tostring(elem, encoding="unicode", method="text")
                if text and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)

    @classmethod
    def extract_scl_from_file(cls, file_path: str) -> str | None:
        """Quick helper: extract just the combined SCL source code."""
        block = cls.parse_file(file_path)
        if block and block.source_code:
            return block.source_code
        return None

    @classmethod
    def is_simaticml(cls, file_path: str) -> bool:
        """Check if a file is a SimaticML XML file."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            # Check for TIA Portal markers
            if root.tag == "Document":
                return True
            for tag in cls.BLOCK_TYPE_MAP:
                if root.find(f".//{tag}") is not None:
                    return True
            if root.find(".//Engineering") is not None:
                return True
            return False
        except ET.ParseError:
            return False
