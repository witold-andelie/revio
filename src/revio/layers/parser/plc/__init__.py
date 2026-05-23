"""PLC parser layer — IEC 61131-3 + vendor XML formats.

Ported from v1's src/plc/ with bug fixes:
- Unified field naming: `datatype` everywhere (v1 had `data_type` in
  st_extractor but `datatype` in plc_rules, causing AttributeError)
- Parse-chain auto-detection: SimaticML → TwinCAT → CODESYS → Rockwell →
  ABB → GE → Omron → generic XML fallback

Public API:
    has_plc_project_extension(path) -> bool
    is_plc_project_file(path) -> bool
    extract_structured_text(path) -> str | None
    StructuredTextExtractor.extract_blocks(source) -> list[STFunctionBlock]
    StructuredTextExtractor.extract_variables(source) -> list[STVariable]

Vendor parsers — each in its own module:
    SimaticMLParser   (Siemens TIA Portal)
    TwincatParser     (Beckhoff TwinCAT 3)
    CodesysParser     (CODESYS V3 family: WAGO, Schneider, ABB AC500)
    RockwellParser    (Rockwell Studio 5000 L5X)
    ABBParser         (ABB Automation Builder)
    GEParser          (GE/Fanuc Proficy Machine Edition)
    OmronParser       (Omron Sysmac Studio .smc2)
    PLCXmlParser      (generic XML fallback)
"""

from .file_support import (
    PLC_PROJECT_EXTENSIONS,
    extract_structured_text,
    has_plc_project_extension,
    is_plc_project_file,
)
from .st_extractor import STFunctionBlock, STVariable, StructuredTextExtractor

__all__ = [
    "PLC_PROJECT_EXTENSIONS",
    "extract_structured_text",
    "has_plc_project_extension",
    "is_plc_project_file",
    "STFunctionBlock",
    "STVariable",
    "StructuredTextExtractor",
]
