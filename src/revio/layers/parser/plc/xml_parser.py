"""Generic PLC XML parser — fallback when no vendor-specific parser matches.

Ported from v1's src/plc/xml_parser.py.

This is a "best-effort" parser for unknown PLC XML dialects. The vendor-
specific parsers (simatic.py, twincat.py, codesys.py, ...) handle the bulk
of real-world inputs. This fallback exists so the file_support detector
can still extract *something* from XMLs we don't fully understand.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel


class PLCProgramBlock(BaseModel):
    """A parsed PLC POU (Program Organization Unit) — generic shape."""

    name: str
    block_type: str              # FB | FC | OB | DB
    language: str                # ST | LD | FBD | SFC | unknown
    source_code: str
    variables: list[dict] = []   # generic: each vendor parser has typed models
    file_path: str = ""


class PLCXmlParser:
    """Best-effort generic PLC XML parser — looks for <POU>/<TcPOU>/<ST> tags."""

    @classmethod
    def parse_file(cls, file_path: str) -> PLCProgramBlock | None:
        """Try multiple generic shapes to extract a POU + ST source."""
        path = Path(file_path)
        if not path.exists():
            return None
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError:
            return None

        # Try shape 1: <POU Name="..." BlockType="..."> with <ST> children
        result = cls._parse_simaticml(root, file_path)
        if result is not None:
            return result

        # Try shape 2: <TcPOU><Name>...</Name>...<Implementation><ST>...
        result = cls._parse_tcpou(root, file_path)
        if result is not None:
            return result

        return None

    @classmethod
    def _parse_simaticml(cls, root: ET.Element, file_path: str) -> PLCProgramBlock | None:
        pou = root.find(".//{*}POU") or root.find(".//POU")
        if pou is None:
            return None

        name = pou.get("Name", "Unknown")
        block_type = pou.get("BlockType", "FB")

        st_sections = pou.findall(".//{*}ST") or pou.findall(".//ST")
        if not st_sections:
            return None

        parts: list[str] = []
        for st in st_sections:
            text = ET.tostring(st, encoding="unicode", method="text")
            if text and text.strip():
                parts.append(text.strip())

        if not parts:
            return None

        return PLCProgramBlock(
            name=name,
            block_type=block_type,
            language="ST",
            source_code="\n\n".join(parts),
            file_path=file_path,
        )

    @classmethod
    def _parse_tcpou(cls, root: ET.Element, file_path: str) -> PLCProgramBlock | None:
        pou = root.find(".//{*}TcPOU") or root.find(".//TcPOU")
        if pou is None:
            return None

        name_elem = pou.find("{*}Name") or pou.find("Name")
        name = name_elem.text if name_elem is not None else "Unknown"

        impl = pou.find(".//{*}Implementation") or pou.find(".//Implementation")
        if impl is None:
            return None

        st_elem = impl.find(".//{*}ST") or impl.find(".//ST")
        if st_elem is None:
            return None

        source = ET.tostring(st_elem, encoding="unicode", method="text")
        if not source or not source.strip():
            return None

        return PLCProgramBlock(
            name=name or "Unknown",
            block_type="FB",
            language="ST",
            source_code=source.strip(),
            file_path=file_path,
        )

    @classmethod
    def extract_st_from_xml(cls, file_path: str) -> str | None:
        block = cls.parse_file(file_path)
        return block.source_code if block else None
