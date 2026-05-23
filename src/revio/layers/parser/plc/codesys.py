"""CODESYS IDE project file parser.

Parses CODESYS V3 project export XML files (.project, .export).
CODESYS is the de facto standard IEC 61131-3 runtime used by 500+ PLC
manufacturers across Europe, including:
  - WAGO (PFC series)
  - Schneider Electric Modicon (M251/M262)
  - ABB AC500
  - Bosch Rexroth (ctrlX)
  - Phoenix Contact (PLCnext)
  - Festo, Lenze, Pilz, KEBA, ifm, and many more

Supports CODESYS V3 XML format with CDATA blocks for declarations
and implementations in all IEC 61131-3 languages (ST, LD, FBD, SFC, IL).

CODESYS V3 project structure:
  <Project xmlns="http://www.codesys.com/project">
    <Device Name="Device">
      <Application Name="Application">
        <POU Name="PLC_PRG" Id="{GUID}">
          <Declaration><![CDATA[
            PROGRAM PLC_PRG
            VAR_INPUT ... END_VAR
            VAR_OUTPUT ... END_VAR
          ]]></Declaration>
          <Implementation>
            <ST><![CDATA[ <code> ]]></ST>
            <!-- or <LD>, <FBD>, <SFC>, <IL> -->
          </Implementation>
        </POU>
        <GlobalVariables Name="GVL">
          <Declaration><![CDATA[ VAR_GLOBAL ... END_VAR ]]></Declaration>
        </GlobalVariables>
        <DataType Name="MyStruct">
          <Declaration><![CDATA[ TYPE MyStruct: STRUCT ... END_STRUCT END_TYPE ]]></Declaration>
        </DataType>
      </Application>
    </Device>
    <Libraries>
      <Library Name="Standard" .../>
    </Libraries>
  </Project>
"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class CodesysVariable(BaseModel):
    """A variable declared in a CODESYS POU."""
    name: str
    datatype: str
    scope: str  # VAR, VAR_INPUT, VAR_OUTPUT, VAR_IN_OUT, VAR_GLOBAL, VAR_EXTERNAL, VAR_TEMP
    address: str | None = None  # AT %I0.0
    initial_value: str | None = None
    comment: str | None = None
    retain: bool = False  # RETAIN keyword


class CodesysPOU(BaseModel):
    """A parsed CODESYS Program Organization Unit."""
    name: str
    pou_type: str  # PROGRAM, FUNCTION_BLOCK, FUNCTION, STRUCT, ENUM
    language: str  # ST, LD, FBD, SFC, IL
    declaration: str  # Raw declaration text from CDATA
    implementation: str  # Raw implementation text from CDATA
    variables: list[CodesysVariable] = []
    file_path: str = ""
    guid: str | None = None
    is_safety: bool = False  # Safety-related POU


class CodesysDevice(BaseModel):
    """A CODESYS device (PLC hardware target)."""
    name: str
    device_type: str = ""  # e.g., "3S CODESYS Control Win V3"
    pou_list: list[CodesysPOU] = []
    global_variables: list[CodesysVariable] = []


class CodesysProject(BaseModel):
    """A parsed CODESYS project."""
    project_name: str = ""
    codesys_version: str | None = None
    devices: list[CodesysDevice] = []
    libraries: list[str] = []
    file_path: str = ""

    @property
    def all_pous(self) -> list[CodesysPOU]:
        """Get all POUs from all devices."""
        return [pou for device in self.devices for pou in device.pou_list]

    @property
    def all_global_variables(self) -> list[CodesysVariable]:
        """Get all global variables from all devices."""
        return [var for device in self.devices for var in device.global_variables]


class CodesysParser:
    """Parse CODESYS IDE project files (V3 XML format)."""

    # CODESYS XML namespaces
    NS = {
        "codesys": "http://www.codesys.com/project",
    }

    @classmethod
    def parse_file(cls, file_path: str) -> CodesysProject | None:
        """Parse a CODESYS project file."""
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
    def _parse_root(cls, root: ET.Element, file_path: str) -> CodesysProject | None:
        """Parse the Project root element."""
        project = CodesysProject(file_path=file_path)

        # Detect CODESYS version
        project.codesys_version = cls._detect_version(root)

        # Get project name
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "TextHeader" and elem.text:
                project.project_name = elem.text.strip()
                break

        # Parse devices and their contents
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "Device":
                device = cls._parse_device(elem)
                if device:
                    project.devices.append(device)

        # If no Device found, try Application directly
        if not project.devices:
            for elem in root.iter():
                tag = cls._local_tag(elem.tag)
                if tag == "Application":
                    device = CodesysDevice(name="Default")
                    cls._parse_application_contents(elem, device)
                    if device.pou_list or device.global_variables:
                        project.devices.append(device)

        # Parse libraries
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "Library":
                lib_name = elem.get("Name", "")
                if lib_name:
                    project.libraries.append(lib_name)

        return project if project.devices else None

    @classmethod
    def _detect_version(cls, root: ET.Element) -> str | None:
        """Detect CODESYS version from file header."""
        # Check FileHeader
        for elem in root.iter():
            tag = cls._local_tag(elem.tag)
            if tag == "FileHeader":
                version = elem.get("SchemaVersion", elem.get("Version"))
                if version:
                    return version

        # Check namespace URI for version hints
        ns_match = re.search(r"codesys.*?(\d+\.\d+)", root.tag, re.IGNORECASE)
        if ns_match:
            return ns_match.group(1)

        return None

    @classmethod
    def _parse_device(cls, device_elem: ET.Element) -> CodesysDevice | None:
        """Parse a Device element."""
        device = CodesysDevice(
            name=device_elem.get("Name", "Unknown"),
            device_type=device_elem.get("Type", ""),
        )

        # Find Application within device
        for child in device_elem:
            tag = cls._local_tag(child.tag)
            if tag == "Application":
                cls._parse_application_contents(child, device)

        return device if device.pou_list or device.global_variables else None

    @classmethod
    def _parse_application_contents(
        cls, app_elem: ET.Element, device: CodesysDevice
    ):
        """Parse contents of an Application element."""
        for child in app_elem:
            tag = cls._local_tag(child.tag)

            if tag == "POU":
                pou = cls._parse_pou(child)
                if pou:
                    device.pou_list.append(pou)

            elif tag == "GlobalVariables":
                gvl = cls._parse_global_variables(child)
                if gvl:
                    device.global_variables.extend(gvl)

            elif tag == "DataType":
                # Treat data type declarations as pseudo-POUs for review
                dt = cls._parse_data_type(child)
                if dt:
                    device.pou_list.append(dt)

            # Recurse into sub-containers
            elif tag in ("Folder", "ObjectGroup", "Container"):
                cls._parse_application_contents(child, device)

    @classmethod
    def _parse_pou(cls, pou_elem: ET.Element) -> CodesysPOU | None:
        """Parse a POU element."""
        name = pou_elem.get("Name", "Unknown")
        guid = pou_elem.get("Id", pou_elem.get("ID"))

        # Determine POU type from declaration text
        declaration = cls._extract_cdata(pou_elem, "Declaration")
        pou_type = cls._detect_pou_type(declaration, name)

        # Determine programming language
        language = cls._detect_language(pou_elem)

        # Extract implementation
        implementation = cls._extract_implementation(pou_elem, language)

        # Parse variables from declaration
        variables = cls._parse_declaration(declaration)

        # Check if safety-related
        is_safety = bool(re.search(
            r"\b(?:Safety|SAFE|SIL|PL[d-e]|Sistema)\b",
            declaration, re.IGNORECASE
        ))

        return CodesysPOU(
            name=name,
            pou_type=pou_type,
            language=language,
            declaration=declaration,
            implementation=implementation,
            variables=variables,
            guid=guid,
            is_safety=is_safety,
        )

    @classmethod
    def _parse_global_variables(
        cls, gvl_elem: ET.Element
    ) -> list[CodesysVariable]:
        """Parse a GlobalVariables element."""
        name = gvl_elem.get("Name", "GVL")
        declaration = cls._extract_cdata(gvl_elem, "Declaration")

        if not declaration:
            return []

        variables = cls._parse_declaration(declaration, scope="VAR_GLOBAL")
        return variables

    @classmethod
    def _parse_data_type(cls, dt_elem: ET.Element) -> CodesysPOU | None:
        """Parse a DataType element (STRUCT, ENUM, etc.)."""
        name = dt_elem.get("Name", "Unknown")
        declaration = cls._extract_cdata(dt_elem, "Declaration")

        if not declaration:
            return None

        return CodesysPOU(
            name=name,
            pou_type="TYPE",
            language="ST",
            declaration=declaration,
            implementation="",
            variables=[],
        )

    @classmethod
    def _extract_cdata(cls, parent: ET.Element, tag_name: str) -> str:
        """Extract text content from an element (handles CDATA)."""
        for child in parent:
            tag = cls._local_tag(child.tag)
            if tag == tag_name:
                text = child.text or ""
                # Also check nested elements
                for sub in child:
                    sub_text = sub.text or ""
                    if sub_text:
                        text += sub_text
                return text.strip()
        return ""

    @classmethod
    def _extract_implementation(
        cls, pou_elem: ET.Element, language: str
    ) -> str:
        """Extract implementation code from a POU."""
        for child in pou_elem:
            tag = cls._local_tag(child.tag)
            if tag == "Implementation":
                # Look for language-specific element
                for lang_elem in child:
                    lang_tag = cls._local_tag(lang_elem.tag)
                    if lang_tag == language or lang_tag == "ST":
                        return (lang_elem.text or "").strip()
                    elif lang_tag in ("LD", "FBD", "SFC"):
                        xml_str = ET.tostring(lang_elem, encoding="unicode")
                        return f"[{lang_tag}_XML:{xml_str}]"
                    elif lang_tag == "IL":
                        return (lang_elem.text or "").strip()

                # Fallback: get all text
                text = ET.tostring(child, encoding="unicode", method="text")
                if text and text.strip():
                    return text.strip()

        return ""

    @classmethod
    def _detect_pou_type(cls, declaration: str, name: str) -> str:
        """Detect POU type from declaration text."""
        if not declaration:
            return "PROGRAM"

        upper = declaration.upper().strip()
        if upper.startswith("PROGRAM"):
            return "PROGRAM"
        elif upper.startswith("FUNCTION_BLOCK"):
            return "FUNCTION_BLOCK"
        elif upper.startswith("FUNCTION"):
            return "FUNCTION"
        elif "STRUCT" in upper:
            return "STRUCT"
        elif "ENUM" in upper:
            return "ENUM"
        return "PROGRAM"

    @classmethod
    def _detect_language(cls, pou_elem: ET.Element) -> str:
        """Detect the programming language of a POU."""
        # Check Implementation sub-elements
        for child in pou_elem:
            tag = cls._local_tag(child.tag)
            if tag == "Implementation":
                for lang_elem in child:
                    lang_tag = cls._local_tag(lang_elem.tag)
                    if lang_tag in ("ST", "LD", "FBD", "SFC", "IL"):
                        return lang_tag

        # Check attributes
        lang = pou_elem.get("Language", pou_elem.get("ProgLang"))
        if lang:
            return lang.upper()

        return "ST"  # Default

    @classmethod
    def _parse_declaration(
        cls, declaration: str, scope: str = "VAR"
    ) -> list[CodesysVariable]:
        """Parse variable declarations from CDATA text."""
        if not declaration:
            return []

        variables = []
        current_scope = scope
        in_retain = False

        lines = declaration.split("\n")

        for line in lines:
            stripped = line.strip()

            if not stripped or stripped.startswith("//") or stripped.startswith("(*"):
                continue

            upper = stripped.upper()

            # Detect scope blocks
            if upper.startswith("VAR_INPUT"):
                current_scope = "VAR_INPUT"
                in_retain = "RETAIN" in upper
                continue
            elif upper.startswith("VAR_OUTPUT"):
                current_scope = "VAR_OUTPUT"
                in_retain = "RETAIN" in upper
                continue
            elif upper.startswith("VAR_IN_OUT"):
                current_scope = "VAR_IN_OUT"
                continue
            elif upper.startswith("VAR_GLOBAL"):
                current_scope = "VAR_GLOBAL"
                in_retain = "RETAIN" in upper
                continue
            elif upper.startswith("VAR_EXTERNAL"):
                current_scope = "VAR_EXTERNAL"
                continue
            elif upper.startswith("VAR_TEMP"):
                current_scope = "VAR_TEMP"
                continue
            elif upper.startswith("VAR_CONSTANT") or upper.startswith("CONSTANT"):
                current_scope = "VAR_CONSTANT"
                continue
            elif upper == "VAR" or upper.startswith("VAR "):
                current_scope = "VAR"
                in_retain = "RETAIN" in upper
                # Check inline declaration
                match = re.match(r"VAR\s+(?:RETAIN\s+)?(\w+)\s*:\s*(\w+.*)", stripped, re.IGNORECASE)
                if match:
                    name = match.group(1)
                    rest = match.group(2)
                    variables.append(cls._parse_var_line(name, rest, current_scope, in_retain))
                continue
            elif upper == "END_VAR":
                current_scope = scope
                in_retain = False
                continue
            elif upper.startswith("END_TYPE"):
                continue

            # Skip POU type declaration line
            if re.match(r"(PROGRAM|FUNCTION_BLOCK|FUNCTION|TYPE)\s+\w+", stripped, re.IGNORECASE):
                continue

            # Parse variable line: name : datatype [AT %addr] [:= value] [// comment]
            match = re.match(r"(\w+)\s*:\s*(.+?)(?:\s*//.*)?$", stripped)
            if match:
                name = match.group(1)
                rest = match.group(2).strip()
                variables.append(cls._parse_var_line(name, rest, current_scope, in_retain))

        return variables

    @classmethod
    def _parse_var_line(
        cls,
        name: str,
        rest: str,
        scope: str,
        retain: bool = False,
    ) -> CodesysVariable:
        """Parse a single variable declaration line."""
        # Extract comment
        comment = None
        comment_match = re.search(r"//(.+)$", rest)
        if comment_match:
            comment = comment_match.group(1).strip()
            rest = rest[:comment_match.start()].strip()

        # Extract AT address
        address = None
        at_match = re.search(r"AT\s+(%[A-Z]\d+(?:\.\d+)?)", rest, re.IGNORECASE)
        if at_match:
            address = at_match.group(1)
            rest = rest[:at_match.start()].strip() + rest[at_match.end():].strip()

        # Extract initial value
        initial_value = None
        init_match = re.search(r":=\s*(.+?)(?:\s*;|\s*$)", rest)
        if init_match:
            initial_value = init_match.group(1).strip()
            rest = rest[:init_match.start()].strip()

        # Clean datatype
        datatype = rest.strip().rstrip(";").strip()

        return CodesysVariable(
            name=name,
            datatype=datatype,
            scope=scope,
            address=address,
            initial_value=initial_value,
            comment=comment,
            retain=retain,
        )

    @classmethod
    def _local_tag(cls, tag: str) -> str:
        """Extract local tag name from potentially namespaced tag."""
        if "}" in tag:
            return tag.split("}")[1]
        return tag

    @classmethod
    def extract_st_from_file(cls, file_path: str) -> str | None:
        """Quick helper: extract all ST implementation code."""
        project = cls.parse_file(file_path)
        if not project:
            return None

        parts = []
        for pou in project.all_pous:
            if pou.implementation and not pou.implementation.startswith("["):
                parts.append(f"// {pou.pou_type} {pou.name}")
                parts.append(pou.implementation)

        return "\n\n".join(parts) if parts else None

    @classmethod
    def is_codesys(cls, file_path: str) -> bool:
        """Check if a file is a CODESYS project file."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            # Check for CODESYS namespace
            if "codesys.com" in root.tag.lower():
                return True

            # Check for CODESYS-specific elements
            for elem in root.iter():
                tag = cls._local_tag(elem.tag)
                if tag in ("ProjectInfo", "Device", "Application"):
                    # Further check for CODESYS markers
                    if root.find(".//{*}ProjectInfo") is not None:
                        return True
                    if elem.get("xmlns", "").lower().find("codesys") >= 0:
                        return True

            # Check file content for CODESYS strings
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(2000)
                    if "codesys" in head.lower() or "3s-smart" in head.lower():
                        return True
            except OSError:
                pass

            return False
        except ET.ParseError:
            return False
