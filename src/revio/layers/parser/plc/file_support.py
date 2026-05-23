"""PLC project file detection and source extraction.

Ported from v1's src/plc/file_support.py.

Entry point for "given a path, is this a PLC artifact and what ST source
does it contain?" The detection chain walks every vendor parser, so a
file with an ambiguous .xml suffix (the most common case — every vendor
ships XML) gets correctly routed.
"""

from __future__ import annotations

from pathlib import Path


# Extensions that COULD be PLC artifacts. .xml is ambiguous (could be
# anything XML), so we must content-detect.
PLC_PROJECT_EXTENSIONS = frozenset({".xml", ".l5x", ".smc2"})


def has_plc_project_extension(file_path: str) -> bool:
    """True iff the path's extension is one we'd consider for PLC content."""
    return Path(file_path).suffix.lower() in PLC_PROJECT_EXTENSIONS


def is_plc_project_file(file_path: str) -> bool:
    """Sniff the file content to confirm it's actually a PLC artifact."""
    # Lazy imports — vendor parsers pull in lxml/zipfile, no need to load
    # them unless the caller actually asks "is this a PLC file?".
    from .abb import ABBParser
    from .codesys import CodesysParser
    from .ge import GEParser
    from .omron import OmronParser
    from .rockwell import RockwellParser
    from .simatic import SimaticMLParser
    from .twincat import TwincatParser
    from .xml_parser import PLCXmlParser

    suffix = Path(file_path).suffix.lower()
    if suffix == ".smc2":
        return OmronParser.is_omron(file_path)
    if suffix == ".l5x":
        return RockwellParser.is_l5x(file_path)
    if suffix != ".xml":
        return False

    # HWConfig is imported separately because it's a static-analysis concern,
    # not a parser. We still want to identify it as PLC content though.
    detectors = [
        SimaticMLParser.is_simaticml,
        TwincatParser.is_twincat,
        CodesysParser.is_codesys,
        RockwellParser.is_l5x,
        ABBParser.is_abb,
        GEParser.is_ge,
        OmronParser.is_omron,
    ]
    try:
        from ...static.plc_hw_config import HWConfigParser
        detectors.append(HWConfigParser.is_hwconfig)
    except ImportError:
        pass  # HW config module may not be installed yet (M4 stage)

    for detector in detectors:
        try:
            if detector(file_path):
                return True
        except Exception:
            continue

    # Last resort: generic PLC XML parser
    try:
        block = PLCXmlParser.parse_file(file_path)
        return bool(block and block.source_code)
    except Exception:
        return False


def extract_structured_text(file_path: str) -> str | None:
    """Pull IEC 61131-3 Structured Text source out of a PLC artifact.

    Tries every vendor parser in priority order. Returns None if nothing
    can be extracted (file format unknown, or file contains only graphical
    diagrams without ST equivalent).
    """
    from .abb import ABBParser
    from .codesys import CodesysParser
    from .ge import GEParser
    from .omron import OmronParser
    from .rockwell import RockwellParser
    from .simatic import SimaticMLParser
    from .twincat import TwincatParser
    from .xml_parser import PLCXmlParser

    suffix = Path(file_path).suffix.lower()

    if suffix == ".smc2":
        extractors = [OmronParser.extract_st_from_file]
    elif suffix == ".l5x":
        extractors = [RockwellParser.extract_st_from_file]
    else:
        extractors = [
            _make_simatic_extractor(SimaticMLParser),
            TwincatParser.extract_st_from_file,
            CodesysParser.extract_st_from_file,
            RockwellParser.extract_st_from_file,
            ABBParser.extract_st_from_file,
            GEParser.extract_st_from_file,
            OmronParser.extract_st_from_file,
            _make_generic_extractor(PLCXmlParser),
        ]

    for extractor in extractors:
        try:
            source = extractor(file_path)
            if source:
                return source
        except Exception:
            continue

    return None


def _make_simatic_extractor(parser_cls):
    """Simatic ST extraction needs custom logic to flatten networks."""
    def extractor(file_path: str) -> str | None:
        if not parser_cls.is_simaticml(file_path):
            return None
        block = parser_cls.parse_file(file_path)
        if not block:
            return None
        if block.source_code:
            return block.source_code
        parts = [n.source_code for n in block.networks if n.source_code]
        return "\n\n".join(parts) if parts else None
    return extractor


def _make_generic_extractor(parser_cls):
    def extractor(file_path: str) -> str | None:
        block = parser_cls.parse_file(file_path)
        return block.source_code if block and block.source_code else None
    return extractor
