"""GE/Fanuc Proficy Machine Edition project parser.

Parses GE PACSystems (RX3i, RX7i, RSTi-EP) project export XML files.
Proficy Machine Edition (PME) is the development environment for GE
programmable automation controllers used in European and US manufacturing.

Structure of a PME project export:
  <ControllerProject>
    <Controller Name="RX3i" Type="IC695CPU310">
      <Configuration>
        <CPU Type="IC695CPU310" Firmware="..." />
      </Configuration>
      <Folder Name="Programs">
        <ProgramBlock Name="Main" Type="PROGRAM">
          <Variables>
            <VarDecl Name="i_Start" Type="BOOL" Direction="INPUT" />
            <VarDecl Name="o_Motor" Type="BOOL" Direction="OUTPUT" />
          </Variables>
          <Code Language="ST">
            <![CDATA[ <ST code> ]]>
          </Code>
        </ProgramBlock>
      </Folder>
      <Folder Name="GlobalVariables">
        <VarDecl Name="g_CycleTime" Type="TIME" DefaultValue="T#100MS" />
      </Folder>
      <Folder Name="FunctionBlocks">
        <ProgramBlock Name="FB_Motor" Type="FUNCTION_BLOCK">
          ...
        </ProgramBlock>
      </Folder>
    </Controller>
  </ControllerProject>
"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GEVariable(BaseModel):
    """A variable in a GE PACSystems program."""
    name: str
    data_type: str
    direction: str = "LOCAL"  # INPUT, OUTPUT, IN_OUT, LOCAL, GLOBAL
    default_value: str | None = None
    address: str | None = None  # %I, %Q, %M, %R, %T, %S
    comment: str | None = None
    retain: bool = False


class GEProgramBlock(BaseModel):
    """A program or function block in a GE project."""
    name: str
    block_type: str  # PROGRAM, FUNCTION_BLOCK, FUNCTION
    language: str = "ST"
    source_code: str = ""
    variables: list[GEVariable] = []
    file_path: str = ""
    folder: str = ""


class GEController(BaseModel):
    """A GE PACSystems controller."""
    name: str
    controller_type: str = ""  # IC695CPU310, IC695CPU320, etc.
    firmware_version: str = ""
    program_blocks: list[GEProgramBlock] = []
    global_variables: list[GEVariable] = []


class GEProject(BaseModel):
    """A parsed GE/Fanuc Proficy Machine Edition project."""
    project_name: str = ""
    controller: GEController | None = None
    file_path: str = ""

    @property
    def all_blocks(self) -> list[GEProgramBlock]:
        """Get all program blocks."""
        if not self.controller:
            return []
        return self.controller.program_blocks

    @property
    def all_variables(self) -> list[GEVariable]:
        """Get all variables."""
        if not self.controller:
            return []
        vars = list(self.controller.global_variables)
        for block in self.controller.program_blocks:
            vars.extend(block.variables)
        return vars


class GEParser:
    """Parse GE/Fanuc Proficy Machine Edition project files."""

    # GE PACSystems controller families
    CONTROLLER_TYPES = {
        "IC695": "PACSystems RX3i",
        "IC698": "PACSystems RX7i",
        "IC693": "Series 90-30",
        "IC200": "VersaMax",
        "IC694": "PACSystems RX3i CPE",
    }

    @classmethod
    def parse_file(cls, file_path: str) -> GEProject | None:
        """Parse a GE PME project export file."""
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
    def _parse_root(cls, root: ET.Element, file_path: str) -> GEProject | None:
        """Parse the root element."""
        project = GEProject(file_path=file_path)

        # Detect format by root element
        root_tag = cls._local_tag(root.tag)

        if root_tag == "ControllerProject":
            return cls._parse_controller_project(root, project, file_path)
        elif root_tag in ("Project", "PACProject", "MachineEdition"):
            return cls._parse_generic_project(root, project, file_path)

        # Try to find GE markers
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("ControllerProject", "PACSystems", "Controller"):
                ctrl_type = elem.get("Type", "")
                if ctrl_type.startswith("IC69") or ctrl_type.startswith("IC200"):
                    return cls._parse_controller_project(root, project, file_path)

        return None

    @classmethod
    def _parse_controller_project(
        cls, root: ET.Element, project: GEProject, file_path: str
    ) -> GEProject | None:
        """Parse ControllerProject format."""
        # Find Controller element
        controller_elem = root.find("Controller")
        if controller_elem is None:
            # Try direct children
            for child in root:
                tag = cls._local_tag(child.tag)
                if tag == "Controller":
                    controller_elem = child
                    break

        if controller_elem is None:
            return None

        ctrl = GEController(
            name=controller_elem.get("Name", "Unknown"),
            controller_type=controller_elem.get("Type", ""),
        )

        # Parse configuration
        config_elem = controller_elem.find("Configuration")
        if config_elem is not None:
            cpu_elem = config_elem.find("CPU")
            if cpu_elem is not None:
                ctrl.controller_type = ctrl.controller_type or cpu_elem.get("Type", "")
                ctrl.firmware_version = cpu_elem.get("Firmware", "")

        # Parse all folders recursively
        cls._parse_folders(controller_elem, ctrl, "")

        project.controller = ctrl
        project.project_name = ctrl.name

        return project if ctrl.program_blocks or ctrl.global_variables else None

    @classmethod
    def _parse_folders(
        cls, parent: ET.Element, ctrl: GEController, folder_path: str
    ):
        """Recursively parse Folder elements."""
        for child in parent:
            tag = cls._local_tag(child.tag)

            if tag == "Folder":
                folder_name = child.get("Name", "")
                current_path = f"{folder_path}/{folder_name}" if folder_path else folder_name

                # Check if this folder contains program blocks
                cls._parse_folder_contents(child, ctrl, current_path)

                # Recurse into sub-folders
                cls._parse_folders(child, ctrl, current_path)

            elif tag == "ProgramBlock":
                block = cls._parse_program_block(child, folder_path)
                if block:
                    ctrl.program_blocks.append(block)

            elif tag == "VarDecl" and not folder_path:
                # Top-level variables (global)
                var = cls._parse_var_decl(child)
                if var:
                    ctrl.global_variables.append(var)

    @classmethod
    def _parse_folder_contents(
        cls, folder_elem: ET.Element, ctrl: GEController, folder_path: str
    ):
        """Parse contents of a Folder element."""
        for child in folder_elem:
            tag = cls._local_tag(child.tag)

            if tag == "ProgramBlock":
                block = cls._parse_program_block(child, folder_path)
                if block:
                    ctrl.program_blocks.append(block)

            elif tag == "VarDecl":
                var = cls._parse_var_decl(child)
                if var:
                    if "global" in folder_path.lower() or "common" in folder_path.lower():
                        var.direction = "GLOBAL"
                        ctrl.global_variables.append(var)

    @classmethod
    def _parse_program_block(
        cls, elem: ET.Element, folder: str
    ) -> GEProgramBlock | None:
        """Parse a ProgramBlock element."""
        name = elem.get("Name", "Unknown")
        block_type = elem.get("Type", "PROGRAM")

        block = GEProgramBlock(
            name=name,
            block_type=block_type,
            folder=folder,
        )

        # Parse variables
        vars_elem = elem.find("Variables")
        if vars_elem is not None:
            for var_elem in vars_elem:
                tag = cls._local_tag(var_elem.tag)
                if tag == "VarDecl":
                    var = cls._parse_var_decl(var_elem)
                    if var:
                        block.variables.append(var)

        # Parse code
        code_elem = elem.find("Code")
        if code_elem is not None:
            block.language = code_elem.get("Language", "ST").upper()
            block.source_code = cls._extract_cdata(code_elem)

        # Also check Implementation element
        impl_elem = elem.find("Implementation")
        if impl_elem is not None and not block.source_code:
            for lang_elem in impl_elem:
                tag = cls._local_tag(lang_elem.tag)
                if tag in ("ST", "IL"):
                    block.source_code = cls._extract_cdata(lang_elem)
                    block.language = tag
                    break
                elif tag in ("LD", "FBD", "SFC"):
                    xml_str = ET.tostring(lang_elem, encoding="unicode")
                    block.source_code = f"[{tag}_XML:{xml_str}]"
                    block.language = tag
                    break

        # Check for StructuredText element (alternative format)
        st_elem = elem.find("StructuredText")
        if st_elem is not None and not block.source_code:
            block.source_code = cls._extract_cdata(st_elem)
            block.language = "ST"

        return block if block.source_code or block.variables else None

    @classmethod
    def _parse_var_decl(cls, elem: ET.Element) -> GEVariable | None:
        """Parse a VarDecl element."""
        name = elem.get("Name", "")
        if not name:
            return None

        data_type = elem.get("Type", elem.get("DataType", "UNKNOWN"))
        direction = elem.get("Direction", elem.get("Scope", "LOCAL"))
        default_value = elem.get("DefaultValue", elem.get("InitialValue"))
        address = elem.get("Address")
        retain = elem.get("Retain", "false").lower() == "true"

        # Extract comment
        comment = None
        comment_elem = elem.find("Comment")
        if comment_elem is not None and comment_elem.text:
            comment = comment_elem.text.strip()

        return GEVariable(
            name=name,
            data_type=data_type,
            direction=direction.upper(),
            default_value=default_value,
            address=address,
            comment=comment,
            retain=retain,
        )

    @classmethod
    def _parse_generic_project(
        cls, root: ET.Element, project: GEProject, file_path: str
    ) -> GEProject | None:
        """Parse generic project format."""
        # Try to find program elements in any structure
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("ProgramBlock", "Program", "POU"):
                block = cls._parse_program_block(elem, "")
                if block:
                    if not project.controller:
                        project.controller = GEController(name="Unknown")
                    project.controller.program_blocks.append(block)

        return project if project.controller and (
            project.controller.program_blocks or project.controller.global_variables
        ) else None

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
    def extract_st_from_file(cls, file_path: str) -> str | None:
        """Quick helper: extract all ST code from a GE project file."""
        project = cls.parse_file(file_path)
        if not project:
            return None

        parts = []
        for block in project.all_blocks:
            if block.source_code and not block.source_code.startswith("["):
                parts.append(f"// {block.block_type} {block.name}")
                parts.append(block.source_code)

        return "\n\n".join(parts) if parts else None

    @classmethod
    def is_ge(cls, file_path: str) -> bool:
        """Check if a file is a GE/Fanuc PME project."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            root_tag = cls._local_tag(root.tag)

            if root_tag in ("ControllerProject", "PACProject", "MachineEdition"):
                return True

            # Check for GE controller type markers
            for elem in root.iter():
                ctrl_type = elem.get("Type", "")
                if ctrl_type.startswith("IC69") or ctrl_type.startswith("IC200"):
                    return True

            # Check file content
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(2000)
                    if "proficy" in head.lower() or "machine edition" in head.lower():
                        return True
                    if "pacsystems" in head.lower() or "ge-fanuc" in head.lower():
                        return True
            except OSError:
                pass

            return False
        except ET.ParseError:
            return False
