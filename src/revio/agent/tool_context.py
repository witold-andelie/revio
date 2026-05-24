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

from ..output.models import Finding


if TYPE_CHECKING:
    from ..layers.parser.call_graph import CallGraph
    from ..layers.parser.function_index import FunctionIndex
    from ..layers.parser.symbol_graph import SymbolGraph
    from ..layers.rag.retriever import GuidelineRetriever
    from ..layers.static.bandit import BanditRunner
    from ..layers.static.clippy import ClippyRunner
    from ..layers.static.cppcheck import CppcheckRunner
    from ..layers.static.detekt import DetektRunner
    from ..layers.static.golangci_lint import GolangCILintRunner
    from ..layers.static.luacheck import LuacheckRunner
    from ..layers.static.oxlint import OxlintRunner
    from ..layers.static.phpstan import PhpstanRunner
    from ..layers.static.rubocop import RubocopRunner
    from ..layers.static.shellcheck import ShellcheckRunner
    from ..layers.static.spotbugs import SpotBugsRunner
    from ..layers.static.sqlfluff import SqlfluffRunner
    from ..layers.static.verilator import VerilatorRunner
    from ..skills import SkillActivation, SkillsRegistry


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
    _bandit_runner: "BanditRunner | None" = field(default=None, init=False, repr=False)
    _bandit_unavailable: bool = field(default=False, init=False, repr=False)
    _clippy_runner: "ClippyRunner | None" = field(default=None, init=False, repr=False)
    _clippy_unavailable: bool = field(default=False, init=False, repr=False)
    _spotbugs_runner: "SpotBugsRunner | None" = field(default=None, init=False, repr=False)
    _spotbugs_unavailable: bool = field(default=False, init=False, repr=False)
    _golangci_runner: "GolangCILintRunner | None" = field(default=None, init=False, repr=False)
    _golangci_unavailable: bool = field(default=False, init=False, repr=False)
    _cppcheck_runner: "CppcheckRunner | None" = field(default=None, init=False, repr=False)
    _cppcheck_unavailable: bool = field(default=False, init=False, repr=False)
    _shellcheck_runner: "ShellcheckRunner | None" = field(default=None, init=False, repr=False)
    _shellcheck_unavailable: bool = field(default=False, init=False, repr=False)
    _luacheck_runner: "LuacheckRunner | None" = field(default=None, init=False, repr=False)
    _luacheck_unavailable: bool = field(default=False, init=False, repr=False)
    _sqlfluff_runner: "SqlfluffRunner | None" = field(default=None, init=False, repr=False)
    _sqlfluff_unavailable: bool = field(default=False, init=False, repr=False)
    _rubocop_runner: "RubocopRunner | None" = field(default=None, init=False, repr=False)
    _rubocop_unavailable: bool = field(default=False, init=False, repr=False)
    _phpstan_runner: "PhpstanRunner | None" = field(default=None, init=False, repr=False)
    _phpstan_unavailable: bool = field(default=False, init=False, repr=False)
    _detekt_runner: "DetektRunner | None" = field(default=None, init=False, repr=False)
    _detekt_unavailable: bool = field(default=False, init=False, repr=False)
    _verilator_runner: "VerilatorRunner | None" = field(default=None, init=False, repr=False)
    _verilator_unavailable: bool = field(default=False, init=False, repr=False)
    _rag_retriever: "GuidelineRetriever | None" = field(default=None, init=False, repr=False)
    _rag_unavailable: bool = field(default=False, init=False, repr=False)
    _skills_registry: "SkillsRegistry | None" = field(default=None, init=False, repr=False)
    _activated_skills: "list[SkillActivation] | None" = field(default=None, init=False, repr=False)

    # Static-analyzer auto-emit buffer. Layer 2 tools (run_bandit / run_oxlint /
    # run_clippy) push their Finding objects here as a side-effect of their
    # invocation. react_node drains this list after each tool call and merges
    # the findings into the agent state. The LLM doesn't have to remember to
    # re-emit them via report_finding — they're guaranteed to appear in the
    # final report.
    pending_findings: list[Finding] = field(default_factory=list, init=False, repr=False)

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

    @property
    def bandit(self) -> "BanditRunner | None":
        if self._bandit_unavailable:
            return None
        if self._bandit_runner is None:
            try:
                from ..layers.static.bandit import BanditRunner

                self._bandit_runner = BanditRunner()
            except Exception as e:
                logger.warning("bandit unavailable: %s", e)
                self._bandit_unavailable = True
                return None
        return self._bandit_runner

    @property
    def clippy(self) -> "ClippyRunner | None":
        if self._clippy_unavailable:
            return None
        if self._clippy_runner is None:
            try:
                from ..layers.static.clippy import ClippyRunner

                self._clippy_runner = ClippyRunner()
            except Exception as e:
                logger.warning("clippy unavailable: %s", e)
                self._clippy_unavailable = True
                return None
        return self._clippy_runner

    @property
    def spotbugs(self) -> "SpotBugsRunner | None":
        if self._spotbugs_unavailable:
            return None
        if self._spotbugs_runner is None:
            try:
                from ..layers.static.spotbugs import SpotBugsRunner

                self._spotbugs_runner = SpotBugsRunner()
            except Exception as e:
                logger.warning("spotbugs unavailable: %s", e)
                self._spotbugs_unavailable = True
                return None
        return self._spotbugs_runner

    @property
    def golangci(self) -> "GolangCILintRunner | None":
        if self._golangci_unavailable:
            return None
        if self._golangci_runner is None:
            try:
                from ..layers.static.golangci_lint import GolangCILintRunner

                self._golangci_runner = GolangCILintRunner()
            except Exception as e:
                logger.warning("golangci-lint unavailable: %s", e)
                self._golangci_unavailable = True
                return None
        return self._golangci_runner

    @property
    def cppcheck(self) -> "CppcheckRunner | None":
        if self._cppcheck_unavailable:
            return None
        if self._cppcheck_runner is None:
            try:
                from ..layers.static.cppcheck import CppcheckRunner

                self._cppcheck_runner = CppcheckRunner()
            except Exception as e:
                logger.warning("cppcheck unavailable: %s", e)
                self._cppcheck_unavailable = True
                return None
        return self._cppcheck_runner

    @property
    def shellcheck(self) -> "ShellcheckRunner | None":
        if self._shellcheck_unavailable:
            return None
        if self._shellcheck_runner is None:
            try:
                from ..layers.static.shellcheck import ShellcheckRunner

                self._shellcheck_runner = ShellcheckRunner()
            except Exception as e:
                logger.warning("shellcheck unavailable: %s", e)
                self._shellcheck_unavailable = True
                return None
        return self._shellcheck_runner

    @property
    def luacheck(self) -> "LuacheckRunner | None":
        if self._luacheck_unavailable:
            return None
        if self._luacheck_runner is None:
            try:
                from ..layers.static.luacheck import LuacheckRunner

                self._luacheck_runner = LuacheckRunner()
            except Exception as e:
                logger.warning("luacheck unavailable: %s", e)
                self._luacheck_unavailable = True
                return None
        return self._luacheck_runner

    @property
    def sqlfluff(self) -> "SqlfluffRunner | None":
        if self._sqlfluff_unavailable:
            return None
        if self._sqlfluff_runner is None:
            try:
                from ..layers.static.sqlfluff import SqlfluffRunner

                self._sqlfluff_runner = SqlfluffRunner()
            except Exception as e:
                logger.warning("sqlfluff unavailable: %s", e)
                self._sqlfluff_unavailable = True
                return None
        return self._sqlfluff_runner

    @property
    def rubocop(self) -> "RubocopRunner | None":
        if self._rubocop_unavailable:
            return None
        if self._rubocop_runner is None:
            try:
                from ..layers.static.rubocop import RubocopRunner

                self._rubocop_runner = RubocopRunner()
            except Exception as e:
                logger.warning("rubocop unavailable: %s", e)
                self._rubocop_unavailable = True
                return None
        return self._rubocop_runner

    @property
    def phpstan(self) -> "PhpstanRunner | None":
        if self._phpstan_unavailable:
            return None
        if self._phpstan_runner is None:
            try:
                from ..layers.static.phpstan import PhpstanRunner

                self._phpstan_runner = PhpstanRunner()
            except Exception as e:
                logger.warning("phpstan unavailable: %s", e)
                self._phpstan_unavailable = True
                return None
        return self._phpstan_runner

    @property
    def detekt(self) -> "DetektRunner | None":
        if self._detekt_unavailable:
            return None
        if self._detekt_runner is None:
            try:
                from ..layers.static.detekt import DetektRunner

                self._detekt_runner = DetektRunner()
            except Exception as e:
                logger.warning("detekt unavailable: %s", e)
                self._detekt_unavailable = True
                return None
        return self._detekt_runner

    @property
    def verilator(self) -> "VerilatorRunner | None":
        if self._verilator_unavailable:
            return None
        if self._verilator_runner is None:
            try:
                from ..layers.static.verilator import VerilatorRunner

                self._verilator_runner = VerilatorRunner()
            except Exception as e:
                logger.warning("verilator unavailable: %s", e)
                self._verilator_unavailable = True
                return None
        return self._verilator_runner

    # ---- Skills ----

    @property
    def skills_registry(self) -> "SkillsRegistry":
        if self._skills_registry is None:
            from ..skills import SkillsRegistry

            self._skills_registry = SkillsRegistry.discover(project_root=self.repo_root)
        return self._skills_registry

    @property
    def activated_skills(self) -> "list[SkillActivation]":
        """Skills that auto-activated based on the project fingerprint."""
        if self._activated_skills is not None:
            return self._activated_skills

        try:
            from ..detect import detect_project

            fp = detect_project(self.repo_root)
            extensions = set(fp.extension_counts.keys())
            languages = set(fp.languages)
            frameworks = set(fp.frameworks)
            filenames = [m for m in fp.markers]
            self._activated_skills = self.skills_registry.activate_for(
                extensions=extensions,
                languages=languages,
                frameworks=frameworks,
                filenames=filenames,
            )
        except Exception as e:
            logger.warning("skill activation failed: %s", e)
            self._activated_skills = []

        return self._activated_skills

    # ---- RAG ----

    @property
    def rag(self) -> "GuidelineRetriever | None":
        """Lazy-load the guidelines retriever. None if no index exists."""
        if self._rag_unavailable:
            return None
        if self._rag_retriever is None:
            try:
                from ..layers.rag.retriever import GuidelineRetriever

                retriever = GuidelineRetriever(repo_root=self.repo_root)
                if not retriever.has_index():
                    # No guidelines indexed; mark unavailable to skip future checks
                    self._rag_unavailable = True
                    return None
                self._rag_retriever = retriever
            except Exception as e:
                logger.warning("RAG unavailable: %s", e)
                self._rag_unavailable = True
                return None
        return self._rag_retriever
