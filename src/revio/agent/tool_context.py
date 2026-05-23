"""Per-session shared context for agent tools.

The Layer 1 + Layer 2 indexes (SymbolGraph / CallGraph / FunctionIndex /
OxlintRunner) are expensive to build but cheap to reuse. Building them
once per session and passing the result to tool factories avoids paying
the cost on every tool call.

Indexes are **lazy** — only built when a tool actually requests them.
A bare `revio review` against a small diff won't pay the indexing cost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from ..layers.parser.call_graph import CallGraph
    from ..layers.parser.function_index import FunctionIndex
    from ..layers.parser.symbol_graph import SymbolGraph
    from ..layers.static.oxlint import OxlintRunner


logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Shared state for a single agent session's tools."""

    repo_root: Path
    profile_name: str

    def __post_init__(self):
        # Always work with the fully-resolved repo root to avoid /tmp ↔
        # /private/tmp drift on macOS (which silently breaks relative_to).
        self.repo_root = Path(self.repo_root).expanduser().resolve()

    # Lazy-built indexes (None until first request)
    _symbol_graph: "SymbolGraph | None" = field(default=None, init=False, repr=False)
    _call_graph: "CallGraph | None" = field(default=None, init=False, repr=False)
    _function_index: "FunctionIndex | None" = field(default=None, init=False, repr=False)
    _oxlint_runner: "OxlintRunner | None" = field(default=None, init=False, repr=False)
    _oxlint_unavailable: bool = field(default=False, init=False, repr=False)

    # ---- Layer 1 ----

    @property
    def symbol_graph(self) -> "SymbolGraph":
        if self._symbol_graph is None:
            from ..layers.parser.symbol_graph import SymbolGraph

            logger.debug("ToolContext: building SymbolGraph for %s", self.repo_root)
            self._symbol_graph = SymbolGraph.build(self.repo_root)
        return self._symbol_graph

    @property
    def call_graph(self) -> "CallGraph":
        if self._call_graph is None:
            from ..layers.parser.call_graph import CallGraph

            self._call_graph = CallGraph.build(self.symbol_graph)
        return self._call_graph

    @property
    def function_index(self) -> "FunctionIndex":
        if self._function_index is None:
            from ..layers.parser.function_index import FunctionIndex

            self._function_index = FunctionIndex.build(self.symbol_graph)
        return self._function_index

    # ---- Layer 2 ----

    @property
    def oxlint(self) -> "OxlintRunner | None":
        if self._oxlint_unavailable:
            return None
        if self._oxlint_runner is None:
            try:
                from ..layers.static.oxlint import OxlintNotInstalledError, OxlintRunner

                self._oxlint_runner = OxlintRunner()
            except Exception as e:
                logger.warning("oxlint unavailable: %s", e)
                self._oxlint_unavailable = True
                return None
        return self._oxlint_runner
