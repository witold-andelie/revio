"""ABB Automation Builder project parser.

Parses ABB Automation Builder project files and program exports.
ABB uses different formats depending on the controller family:

  - AC500 (PM5xx) / AC500-eCo: CODESYS-based → handled by CodesysParser
  - AC500-S (Safety): CODESYS-based with safety extensions
  - ABB Program Export: XML format with <ABBProject> root
  - Automation Builder (.abbproj): XML project file

For CODESYS-based ABB controllers, this parser delegates to CodesysParser.
For ABB-specific formats, it provides direct parsing.

Structure of an ABB Program Export:
  <ABBProject Version="1.0">
    <ProjectInfo>
      <Name>MyProject</Name>
      <ControllerType>PM5560</ControllerType>
    </ProjectInfo>
    <Programs>
      <Program Name="Main" Type="PROGRAM">
        <Variables>
          <Variable Name="i_Start" Direction="Input" DataType="BOOL"/>
          <Variable Name="o_Motor" Direction="Output" DataType="BOOL"/>
        </Variables>
        <Source Language="ST">
          <![CDATA[
            o_Motor := i_Start AND NOT i_Stop;
          ]]>
        </Source>
      </Program>
    </Programs>
    <FunctionBlocks>
      <FunctionBlock Name="FB_Motor" Type="FUNCTION_BLOCK">
        <Variables>...</Variables>
        <Source Language="ST">...</Source>
      </FunctionBlock>
    </FunctionBlocks>
    <GlobalVariables>
      <Variable Name="g_CycleTime" DataType="TIME" Value="T#100MS"/>
    </GlobalVariables>
  </ABBProject>
"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ABBVariable(BaseModel):
    """A variable in an ABB program."""
    name: str
    data_type: str
    direction: str = "Local"  # Input, Output, InOut, Local, Global
    initial_value: str | None = None
    address: str | None = None  # Physical address mapping
    comment: str | None = None
    retain: bool = False


class ABBProgramBlock(BaseModel):
    """A program or function block in an ABB project."""
    name: str
    block_type: str  # PROGRAM, FUNCTION_BLOCK, FUNCTION
    language: str = "ST"
    source_code: str = ""
    variables: list[ABBVariable] = []
    file_path: str = ""


class ABBProject(BaseModel):
    """A parsed ABB Automation Builder project."""
    project_name: str = ""
    controller_type: str = ""  # PM5560, PM5630, etc.
    controller_family: str = ""  # AC500, AC500-eCo, AC500-S
    programs: list[ABBProgramBlock] = []
    function_blocks: list[ABBProgramBlock] = []
    global_variables: list[ABBVariable] = []
    file_path: str = ""
    is_codesys_based: bool = False

    @property
    def all_blocks(self) -> list[ABBProgramBlock]:
        """Get all program blocks."""
        return self.programs + self.function_blocks


class ABBParser:
    """Parse ABB Automation Builder project files."""

    # ABB controller families
    CONTROLLER_FAMILIES = {
        "PM55": "AC500",
        "PM56": "AC500",
        "PM57": "AC500",
        "PM58": "AC500",
        "PM59": "AC500",
        "PM53": "AC500-eCo",
        "PM554": "AC500-eCo",
        "PM556": "AC500-eCo",
        "PM564": "AC500-eCo",
        "PM574": "AC500-eCo",
        "PM583": "AC500-eCo",
    }

    @classmethod
    def parse_file(cls, file_path: str) -> ABBProject | None:
        """Parse an ABB project file."""
        path = Path(file_path)
        if not path.exists():
            return None

        # Check if it's actually a CODESYS-based ABB project
        if cls._is_codesys_abb(file_path):
            return cls._parse_codesys_abb(file_path)

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f"XML parse error in {file_path}: {e}")
            return None

        return cls._parse_root(root, file_path)

    @classmethod
    def _parse_root(cls, root: ET.Element, file_path: str) -> ABBProject | None:
        """Parse the ABB XML root element."""
        project = ABBProject(file_path=file_path)

        # Detect format by root element
        root_tag = cls._local_tag(root.tag)

        if root_tag == "ABBProject":
            return cls._parse_abb_project(root, project, file_path)
        elif root_tag in ("Project", "AutomationProject"):
            return cls._parse_automation_project(root, project, file_path)

        # Try to find ABB markers in any format
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("ABBProject", "AutomationBuilder", "AC500Project"):
                return cls._parse_abb_project(elem, project, file_path)

        return None

    @classmethod
    def _parse_abb_project(
        cls, root: ET.Element, project: ABBProject, file_path: str
    ) -> ABBProject | None:
        """Parse ABBProject format."""
        # Project info
        info_elem = root.find("ProjectInfo")
        if info_elem is not None:
            name_elem = info_elem.find("Name")
            if name_elem is not None and name_elem.text:
                project.project_name = name_elem.text.strip()

            ctrl_elem = info_elem.find("ControllerType")
            if ctrl_elem is not None and ctrl_elem.text:
                project.controller_type = ctrl_elem.text.strip()
                project.controller_family = cls._detect_family(project.controller_type)

        # Version attribute
        version = root.get("Version")
        if version:
            project.project_name = project.project_name or f"ABB Project v{version}"

        # Parse programs
        programs_elem = root.find("Programs")
        if programs_elem is not None:
            for prog_elem in programs_elem:
                tag = cls._local_tag(prog_elem.tag)
                if tag in ("Program", "FunctionBlock", "Function"):
                    block = cls._parse_program_block(prog_elem)
                    if block:
                        if tag == "Program":
                            project.programs.append(block)
                        else:
                            project.function_blocks.append(block)

        # Parse FunctionBlocks (separate section)
        fb_elem = root.find("FunctionBlocks")
        if fb_elem is not None:
            for block_elem in fb_elem:
                tag = cls._local_tag(block_elem.tag)
                if tag in ("FunctionBlock", "Function"):
                    block = cls._parse_program_block(block_elem)
                    if block:
                        project.function_blocks.append(block)

        # Parse global variables
        gv_elem = root.find("GlobalVariables")
        if gv_elem is not None:
            project.global_variables = cls._parse_variables(gv_elem, "Global")

        return project if project.all_blocks or project.global_variables else None

    @classmethod
    def _parse_automation_project(
        cls, root: ET.Element, project: ABBProject, file_path: str
    ) -> ABBProject | None:
        """Parse generic AutomationProject format."""
        # Try to find program elements in any structure
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("Program", "POU", "ProgramBlock"):
                block = cls._parse_program_block(elem)
                if block:
                    project.programs.append(block)
            elif tag == "GlobalVariables":
                gvs = cls._parse_variables(elem, "Global")
                project.global_variables.extend(gvs)

        return project if project.all_blocks or project.global_variables else None

    @classmethod
    def _parse_program_block(cls, elem: ET.Element) -> ABBProgramBlock | None:
        """Parse a Program/FunctionBlock/Function element."""
        name = elem.get("Name", "Unknown")
        block_type = cls._local_tag(elem.tag).upper()
        if block_type == "PROGRAM":
            block_type = "PROGRAM"
        elif block_type in ("FUNCTIONBLOCK", "FUNCTION_BLOCK"):
            block_type = "FUNCTION_BLOCK"
        elif block_type == "FUNCTION":
            block_type = "FUNCTION"

        # Detect language
        language = "ST"
        lang_attr = elem.get("Language", elem.get("Type", ""))
        if lang_attr:
            language = lang_attr.upper()

        block = ABBProgramBlock(
            name=name,
            block_type=block_type,
            language=language,
            file_path="",
        )

        # Parse variables
        var_elem = elem.find("Variables")
        if var_elem is not None:
            block.variables = cls._parse_variables(var_elem)

        # Parse source code
        source_elem = elem.find("Source")
        if source_elem is not None:
            lang_inner = source_elem.get("Language", "")
            if lang_inner:
                block.language = lang_inner.upper()

            # Check for language-specific sub-elements
            st_elem = source_elem.find("ST")
            if st_elem is not None:
                block.source_code = cls._extract_cdata(st_elem)
            elif source_elem.text:
                block.source_code = source_elem.text.strip()
            else:
                # Try CDATA in the Source element itself
                block.source_code = cls._extract_cdata(source_elem)

        # Also check Implementation element (IEC 61131-3 style)
        impl_elem = elem.find("Implementation")
        if impl_elem is not None and not block.source_code:
            for lang_elem in impl_elem:
                tag = cls._local_tag(lang_elem.tag)
                if tag in ("ST", "IL"):
                    block.source_code = cls._extract_cdata(lang_elem)
                    break
                elif tag in ("LD", "FBD", "SFC"):
                    xml_str = ET.tostring(lang_elem, encoding="unicode")
                    block.source_code = f"[{tag}_XML:{xml_str}]"
                    break

        # Declaration element
        decl_elem = elem.find("Declaration")
        if decl_elem is not None and not block.variables:
            decl_text = cls._extract_cdata(decl_elem)
            if decl_text:
                block.variables = cls._parse_declaration_text(decl_text)

        return block if block.source_code or block.variables else None

    @classmethod
    def _parse_variables(
        cls, var_elem: ET.Element, default_scope: str = "Local"
    ) -> list[ABBVariable]:
        """Parse Variables section."""
        variables = []

        for v_elem in var_elem:
            tag = cls._local_tag(v_elem.tag)
            if tag != "Variable":
                continue

            name = v_elem.get("Name", "")
            if not name:
                continue

            data_type = v_elem.get("DataType", v_elem.get("Type", "UNKNOWN"))
            direction = v_elem.get("Direction", v_elem.get("Scope", default_scope))
            address = v_elem.get("Address")
            initial = v_elem.get("Value", v_elem.get("InitialValue"))
            retain = v_elem.get("Retain", "false").lower() == "true"

            # Extract comment
            comment = None
            comment_elem = v_elem.find("Comment")
            if comment_elem is not None and comment_elem.text:
                comment = comment_elem.text.strip()

            variables.append(ABBVariable(
                name=name,
                data_type=data_type,
                direction=direction,
                initial_value=initial,
                address=address,
                comment=comment,
                retain=retain,
            ))

        return variables

    @classmethod
    def _parse_declaration_text(cls, text: str) -> list[ABBVariable]:
        """Parse IEC 61131-3 declaration text."""
        variables = []
        current_scope = "Local"

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue

            upper = stripped.upper()

            # Detect scope
            if upper.startswith("VAR_INPUT"):
                current_scope = "Input"
                continue
            elif upper.startswith("VAR_OUTPUT"):
                current_scope = "Output"
                continue
            elif upper.startswith("VAR_IN_OUT"):
                current_scope = "InOut"
                continue
            elif upper.startswith("VAR_GLOBAL"):
                current_scope = "Global"
                continue
            elif upper == "END_VAR":
                current_scope = "Local"
                continue

            # Parse: name : datatype [:= value]
            match = re.match(r"(\w+)\s*:\s*(\w+)(?:\s*:=\s*(.+?))?;", stripped)
            if match:
                variables.append(ABBVariable(
                    name=match.group(1),
                    data_type=match.group(2),
                    direction=current_scope,
                    initial_value=match.group(3),
                ))

        return variables

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
    def _detect_family(cls, controller_type: str) -> str:
        """Detect controller family from type string."""
        for prefix, family in cls.CONTROLLER_FAMILIES.items():
            if controller_type.startswith(prefix):
                return family
        return "AC500"

    @classmethod
    def _is_codesys_abb(cls, file_path: str) -> bool:
        """Check if file is a CODESYS-based ABB project."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(3000)
                # ABB CODESYS projects often have ABB-specific markers
                if "abb" in head.lower() and "codesys" in head.lower():
                    return True
                if "ac500" in head.lower() or "pm5" in head.lower():
                    return True
        except OSError:
            pass
        return False

    @classmethod
    def _parse_codesys_abb(cls, file_path: str) -> ABBProject | None:
        """Parse CODESYS-based ABB project via CodesysParser."""
        from .codesys_parser import CodesysParser

        codesys_project = CodesysParser.parse_file(file_path)
        if not codesys_project:
            return None

        project = ABBProject(
            project_name=codesys_project.project_name,
            file_path=file_path,
            is_codesys_based=True,
        )

        # Detect controller type from device info
        for device in codesys_project.devices:
            if device.device_type:
                project.controller_type = device.device_type
                project.controller_family = cls._detect_family(device.device_type)

        # Convert CODESYS POUs to ABB blocks
        for pou in codesys_project.all_pous:
            if pou.implementation and not pou.implementation.startswith("["):
                block = ABBProgramBlock(
                    name=pou.name,
                    block_type=pou.pou_type,
                    language=pou.language,
                    source_code=pou.implementation,
                    variables=[
                        ABBVariable(
                            name=v.name,
                            data_type=v.datatype,
                            direction=v.scope.replace("VAR_", "").replace("VAR", "Local"),
                            initial_value=v.initial_value,
                            address=v.address,
                            comment=v.comment,
                            retain=v.retain,
                        )
                        for v in pou.variables
                    ],
                    file_path=file_path,
                )
                if block.block_type == "PROGRAM":
                    project.programs.append(block)
                else:
                    project.function_blocks.append(block)

        return project if project.all_blocks else None

    @classmethod
    def _local_tag(cls, tag: str) -> str:
        """Extract local tag name from namespaced tag."""
        if "}" in tag:
            return tag.split("}")[1]
        return tag

    @classmethod
    def extract_st_from_file(cls, file_path: str) -> str | None:
        """Quick helper: extract all ST code from an ABB project file."""
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
    def is_abb(cls, file_path: str) -> bool:
        """Check if a file is an ABB Automation Builder project."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            root_tag = cls._local_tag(root.tag)

            if root_tag in ("ABBProject", "AC500Project", "AutomationProject"):
                return True

            # Check for ABB markers in content
            for elem in root.iter():
                tag = cls._local_tag(elem.tag)
                if tag in ("ABBProject", "AutomationBuilder"):
                    return True

            # Check file content
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(2000)
                    if "automation builder" in head.lower():
                        return True
                    if "ac500" in head.lower() and "abb" in head.lower():
                        return True
            except OSError:
                pass

            return False
        except ET.ParseError:
            return False
