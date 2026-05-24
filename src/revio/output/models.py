"""Core output models.

Designed around the **hypothesis → evidence** pattern, not flat "findings list".
Every Finding tracks how the agent arrived at it, so users can audit the reasoning.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ReviewCategory(str, Enum):
    CODE_STYLE = "code_style"
    POTENTIAL_BUG = "potential_bug"
    SECURITY = "security"
    ARCHITECTURE = "architecture"
    READABILITY = "readability"
    CONVENTION = "convention"
    PERFORMANCE = "performance"
    # dedup-specific
    REDUNDANCY = "redundancy"


EvidenceKind = Literal["tool_call", "reasoning", "code_excerpt", "static_rule", "cross_reference"]


class Evidence(BaseModel):
    """One link in the chain of reasoning that supports a Finding."""

    kind: EvidenceKind
    summary: str = Field(max_length=300, description="One-line description (shown in stream)")
    detail: str | None = Field(default=None, description="Full content (shown on --verbose)")
    source: str | None = Field(
        default=None,
        description="Where this came from: file path, tool name, rule id, etc.",
    )


class Finding(BaseModel):
    """A single review finding with hypothesis-evidence trace."""

    # --- Location ---
    file_path: str
    line_start: int
    line_end: int | None = None

    # --- Classification ---
    severity: Severity
    category: ReviewCategory

    # --- Content ---
    title: str = Field(max_length=200)
    hypothesis: str = Field(
        description="What the agent claims is wrong (the assertion being made)"
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="Chain of reasoning + tool calls that support the hypothesis",
    )
    counter_considered: str | None = Field(
        default=None,
        description="Alternative interpretations the agent considered and ruled out",
    )

    # --- Disposition ---
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    verified: bool = Field(
        default=False, description="Whether a self-verify pass was run on this finding"
    )
    suggestion: str | None = None

    # --- Provenance ---
    detected_by: Literal["agent", "static", "external", "advanced"] = "agent"


class ReviewReport(BaseModel):
    """Complete review report."""

    # --- Headline ---
    summary: str
    findings: list[Finding] = Field(default_factory=list)

    # --- Coverage ---
    reviewed_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)

    # --- Session stats (agentic, not just per-file) ---
    tool_calls_used: int = 0
    tool_calls_budget: int = 0
    duration_seconds: float = 0.0
    model_used: str = ""

    # --- LLM accounting (filled by runner.py from usage_metadata) ---
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0
    est_cost_usd: float = 0.0

    # --- Cross-finding observations (the "reflect" node's output) ---
    systemic_observations: list[str] = Field(
        default_factory=list,
        description="Patterns the agent noticed across multiple findings",
    )

    # --- Severity rollup (computed) ---
    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.INFO)

    def stats_dict(self) -> dict[str, int]:
        return {
            "critical": self.critical_count,
            "error": self.error_count,
            "warning": self.warning_count,
            "info": self.info_count,
        }


# --- Convenience constructors --------------------------------------------------


def make_evidence_reasoning(summary: str, detail: str | None = None) -> Evidence:
    return Evidence(kind="reasoning", summary=summary, detail=detail)


def make_evidence_tool(tool_name: str, summary: str, detail: str | None = None) -> Evidence:
    return Evidence(kind="tool_call", summary=summary, detail=detail, source=tool_name)


def make_evidence_code(file_path: str, summary: str, code: str) -> Evidence:
    return Evidence(kind="code_excerpt", summary=summary, detail=code, source=file_path)
