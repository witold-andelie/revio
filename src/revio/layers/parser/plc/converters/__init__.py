"""PLC graphical-language → Structured Text converters.

Many real PLC programs are written in graphical languages (Ladder Diagram,
Function Block Diagram, Sequential Function Chart) rather than ST. To analyze
them with the same toolchain that handles ST, we convert them first.

Three converters, ported from v1:
    LadderDiagramConverter — LD relay logic → ST (AOV graph + topological sort)
    FBDConverter           — FBD data flow → ST (Kahn's topological sort)
    SFCConverter           — SFC state machines → ST CASE-based logic
"""

from .fbd import FBDConverter
from .ladder import LadderDiagramConverter
from .sfc import SFCConverter

__all__ = ["FBDConverter", "LadderDiagramConverter", "SFCConverter"]
