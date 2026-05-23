"""Omron Sysmac Studio project parser.

Parses Omron Sysmac Studio project files (.smc2) and XML exports.
Sysmac Studio is the development environment for Omron NJ/NX/NY series
machine automation controllers used in European and Asian manufacturing.

.smc2 files are ZIP archives containing XML project files.
This parser handles both:
  1. Direct XML exports from Sysmac Studio
  2. Internal XML files extracted from .smc2 archives

Structure of a Sysmac Studio XML export:
  <SysmacStudioProject Version="1.0">
    <ProjectInfo>
      <ControllerType>NJ501-1500</ControllerType>
    </ProjectInfo>
    <TaskConfiguration>
      <Task Name="MainTask" Type="Periodic" Interval="T#10MS">
        <Program Name="MainProgram" />
      </Task>
    </TaskConfiguration>
    <Programs>
      <Program Name="MainProgram">
        <Variables>
          <Variable Name="i_Start" DataType="BOOL" Usage="Input" />
          <Variable Name="o_Motor" DataType="BOOL" Usage="Output" />
        </Variables>
        <Implementation>
          <ST><![CDATA[ <ST code> ]]></ST>
        </Implementation>
      </Program>
    </Programs>
    <FunctionBlocks>
      <FunctionBlock Name="FB_Motor">
        <Variables>...</Variables>
        <Implementation>
          <ST><![CDATA[ <ST code> ]]></ST>
        </Implementation>
      </FunctionBlock>
    </FunctionBlocks>
    <GlobalVariables>
      <Variable Name="g_CycleTime" DataType="TIME" InitialValue="T#10MS" />
    </GlobalVariables>
  </SysmacStudioProject>
"""

import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class OmronVariable(BaseModel):
    """A variable in an Omron program."""
    name: str
    data_type: str
    usage: str = "Internal"  # Input, Output, Internal, Global
    initial_value: str | None = None
    address: str | None = None  # W0.0, D100, etc.
    comment: str | None = None
    retain: bool = False
    at_global: bool = False


class OmronProgram(BaseModel):
    """A program or function block in an Omron project."""
    name: str
    block_type: str  # Program, FunctionBlock, Function
    language: str = "ST"
    source_code: str = ""
    variables: list[OmronVariable] = []
    file_path: str = ""
    task_name: str | None = None


class OmronTask(BaseModel):
    """A task configuration."""
    name: str
    task_type: str = "Periodic"  # Periodic, Event, Constant
    interval: str | None = None  # e.g., T#10MS
    programs: list[str] = []  # Program names assigned to this task


class OmronProject(BaseModel):
    """A parsed Omron Sysmac Studio project."""
    project_name: str = ""
    controller_type: str = ""  # NJ501-1500, NX102, NY532, etc.
    controller_family: str = ""  # NJ, NX, NY
    programs: list[OmronProgram] = []
    function_blocks: list[OmronProgram] = []
    global_variables: list[OmronVariable] = []
    tasks: list[OmronTask] = []
    file_path: str = ""

    @property
    def all_blocks(self) -> list[OmronProgram]:
        """Get all program blocks."""
        return self.programs + self.function_blocks


class OmronParser:
    """Parse Omron Sysmac Studio project files."""

    # Omron controller families
    CONTROLLER_FAMILIES = {
        "NJ": "NJ Series (Machine Automation)",
        "NX": "NX Series (Machine Automation)",
        "NY": "NY Series (Industrial PC)",
        "CP": "CP Series (Compact)",
        "CJ": "CJ Series (Modular)",
        "CS": "CS Series (Process)",
    }

    @classmethod
    def parse_file(cls, file_path: str) -> OmronProject | None:
        """Parse an Omron Sysmac Studio project file."""
        path = Path(file_path)
        if not path.exists():
            return None

        # Check if it's a ZIP archive (.smc2)
        if path.suffix.lower() == ".smc2":
            return cls._parse_smc2(file_path)

        # Try as XML
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f"XML parse error in {file_path}: {e}")
            return None

        return cls._parse_root(root, file_path)

    @classmethod
    def _parse_smc2(cls, file_path: str) -> OmronProject | None:
        """Parse an Omron .smc2 ZIP archive."""
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                # Look for project XML files inside the archive
                project_files = [
                    f for f in zf.namelist()
                    if f.endswith(('.xml', '.XML')) and not f.startswith('__MACOSX')
                ]

                # Try to find the main project file
                main_file = None
                for pf in project_files:
                    if 'project' in pf.lower() or 'main' in pf.lower():
                        main_file = pf
                        break

                if not main_file and project_files:
                    main_file = project_files[0]

                if not main_file:
                    return None

                # Parse the main project file
                with zf.open(main_file) as f:
                    tree = ET.parse(f)
                    root = tree.getroot()

                project = cls._parse_root(root, file_path)

                # Also parse any additional program files
                if project:
                    for pf in project_files:
                        if pf == main_file:
                            continue
                        try:
                            with zf.open(pf) as f:
                                sub_tree = ET.parse(f)
                                sub_root = sub_tree.getroot()
                                cls._extract_additional_programs(sub_root, project)
                        except ET.ParseError:
                            continue

                return project

        except (zipfile.BadZipFile, OSError) as e:
            logger.warning(f"Failed to open {file_path}: {e}")
            return None

    @classmethod
    def _parse_root(cls, root: ET.Element, file_path: str) -> OmronProject | None:
        """Parse the XML root element."""
        project = OmronProject(file_path=file_path)
        root_tag = cls._local_tag(root.tag)

        if root_tag == "SysmacStudioProject":
            return cls._parse_sysmac_project(root, project, file_path)
        elif root_tag in ("Project", "OmronProject", "NXProject"):
            return cls._parse_generic_project(root, project, file_path)

        # Try to find Omron markers
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("SysmacStudio", "OmronProject", "NXProject"):
                return cls._parse_sysmac_project(root, project, file_path)

        return None

    @classmethod
    def _parse_sysmac_project(
        cls, root: ET.Element, project: OmronProject, file_path: str
    ) -> OmronProject | None:
        """Parse SysmacStudioProject format."""
        # Project info
        info_elem = root.find("ProjectInfo")
        if info_elem is not None:
            ctrl_elem = info_elem.find("ControllerType")
            if ctrl_elem is not None and ctrl_elem.text:
                project.controller_type = ctrl_elem.text.strip()
                project.controller_family = cls._detect_family(project.controller_type)

            name_elem = info_elem.find("ProjectName")
            if name_elem is not None and name_elem.text:
                project.project_name = name_elem.text.strip()

        # Parse task configuration
        task_elem = root.find("TaskConfiguration")
        if task_elem is not None:
            project.tasks = cls._parse_tasks(task_elem)

        # Build task→program mapping
        task_map = {}
        for task in project.tasks:
            for prog_name in task.programs:
                task_map[prog_name] = task.name

        # Parse programs
        programs_elem = root.find("Programs")
        if programs_elem is not None:
            for prog_elem in programs_elem:
                tag = cls._local_tag(prog_elem.tag)
                if tag == "Program":
                    prog = cls._parse_program(prog_elem, "Program")
                    if prog:
                        prog.task_name = task_map.get(prog.name)
                        project.programs.append(prog)

        # Parse function blocks
        fb_elem = root.find("FunctionBlocks")
        if fb_elem is not None:
            for fb_elem_child in fb_elem:
                tag = cls._local_tag(fb_elem_child.tag)
                if tag == "FunctionBlock":
                    fb = cls._parse_program(fb_elem_child, "FunctionBlock")
                    if fb:
                        project.function_blocks.append(fb)

        # Parse global variables
        gv_elem = root.find("GlobalVariables")
        if gv_elem is not None:
            project.global_variables = cls._parse_variables(gv_elem, "Global")

        return project if project.all_blocks or project.global_variables else None

    @classmethod
    def _parse_generic_project(
        cls, root: ET.Element, project: OmronProject, file_path: str
    ) -> OmronProject | None:
        """Parse generic project format."""
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag in ("Program", "POU"):
                prog = cls._parse_program(elem, "Program")
                if prog:
                    project.programs.append(prog)
            elif tag == "FunctionBlock":
                fb = cls._parse_program(elem, "FunctionBlock")
                if fb:
                    project.function_blocks.append(fb)
            elif tag == "GlobalVariables":
                gvs = cls._parse_variables(elem, "Global")
                project.global_variables.extend(gvs)

        return project if project.all_blocks or project.global_variables else None

    @classmethod
    def _parse_tasks(cls, task_elem: ET.Element) -> list[OmronTask]:
        """Parse TaskConfiguration section."""
        tasks = []
        for child in task_elem:
            tag = cls._local_tag(child.tag)
            if tag == "Task":
                task = OmronTask(
                    name=child.get("Name", "Unknown"),
                    task_type=child.get("Type", "Periodic"),
                    interval=child.get("Interval"),
                )
                # Find assigned programs
                for prog_ref in child:
                    ref_tag = cls._local_tag(prog_ref.tag)
                    if ref_tag == "Program":
                        prog_name = prog_ref.get("Name", prog_ref.text or "")
                        if prog_name:
                            task.programs.append(prog_name)
                tasks.append(task)
        return tasks

    @classmethod
    def _parse_program(
        cls, elem: ET.Element, block_type: str
    ) -> OmronProgram | None:
        """Parse a Program or FunctionBlock element."""
        name = elem.get("Name", "Unknown")

        prog = OmronProgram(
            name=name,
            block_type=block_type,
        )

        # Parse variables
        var_elem = elem.find("Variables")
        if var_elem is not None:
            prog.variables = cls._parse_variables(var_elem)

        # Parse implementation
        impl_elem = elem.find("Implementation")
        if impl_elem is not None:
            for lang_elem in impl_elem:
                tag = cls._local_tag(lang_elem.tag)
                if tag in ("ST", "IL"):
                    prog.source_code = cls._extract_cdata(lang_elem)
                    prog.language = tag
                    break
                elif tag in ("LD", "FBD", "SFC"):
                    xml_str = ET.tostring(lang_elem, encoding="unicode")
                    prog.source_code = f"[{tag}_XML:{xml_str}]"
                    prog.language = tag
                    break

        # Alternative: Source element
        source_elem = elem.find("Source")
        if source_elem is not None and not prog.source_code:
            prog.source_code = cls._extract_cdata(source_elem)
            prog.language = source_elem.get("Language", "ST")

        # Alternative: Code element
        code_elem = elem.find("Code")
        if code_elem is not None and not prog.source_code:
            prog.source_code = cls._extract_cdata(code_elem)
            prog.language = code_elem.get("Language", "ST")

        return prog if prog.source_code or prog.variables else None

    @classmethod
    def _parse_variables(
        cls, var_elem: ET.Element, default_usage: str = "Internal"
    ) -> list[OmronVariable]:
        """Parse Variables section."""
        variables = []
        for child in var_elem:
            tag = cls._local_tag(child.tag)
            if tag == "Variable":
                name = child.get("Name", "")
                if not name:
                    continue

                variables.append(OmronVariable(
                    name=name,
                    data_type=child.get("DataType", child.get("Type", "UNKNOWN")),
                    usage=child.get("Usage", child.get("Scope", default_usage)),
                    initial_value=child.get("InitialValue", child.get("Value")),
                    address=child.get("Address"),
                    comment=cls._get_comment(child),
                    retain=child.get("Retain", "false").lower() == "true",
                ))
        return variables

    @classmethod
    def _extract_additional_programs(
        cls, root: ET.Element, project: OmronProject
    ):
        """Extract additional programs from sub-files in ZIP."""
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "Program":
                prog = cls._parse_program(elem, "Program")
                if prog and not any(p.name == prog.name for p in project.programs):
                    project.programs.append(prog)
            elif tag == "FunctionBlock":
                fb = cls._parse_program(elem, "FunctionBlock")
                if fb and not any(p.name == fb.name for p in project.function_blocks):
                    project.function_blocks.append(fb)

    @classmethod
    def _get_comment(cls, elem: ET.Element) -> str | None:
        """Extract comment from element."""
        comment_elem = elem.find("Comment")
        if comment_elem is not None and comment_elem.text:
            return comment_elem.text.strip()
        return None

    @classmethod
    def _detect_family(cls, controller_type: str) -> str:
        """Detect controller family from type string."""
        for prefix, family in cls.CONTROLLER_FAMILIES.items():
            if controller_type.upper().startswith(prefix):
                return family
        return "Unknown"

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
        """Quick helper: extract all ST code from an Omron project file."""
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
    def is_omron(cls, file_path: str) -> bool:
        """Check if a file is an Omron Sysmac Studio project."""
        path = Path(file_path)

        # Check file extension
        if path.suffix.lower() == ".smc2":
            return True

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            root_tag = cls._local_tag(root.tag)

            if root_tag in ("SysmacStudioProject", "NXProject", "OmronProject"):
                return True

            # Check for Omron markers
            for elem in root.iter():
                tag = cls._local_tag(elem.tag)
                if tag in ("SysmacStudio", "OmronProject"):
                    return True
                ctrl_type = elem.get("ControllerType", "")
                if any(ctrl_type.upper().startswith(p) for p in ("NJ", "NX", "NY")):
                    return True

            # Check file content
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(2000)
                    if "sysmac" in head.lower() or "omron" in head.lower():
                        return True
            except OSError:
                pass

            return False
        except ET.ParseError:
            return False
