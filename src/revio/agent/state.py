"""Agent state schema.

The state object flows through every node in the LangGraph. Reducers control
how concurrent updates from multiple nodes are merged.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from ..output.models import Finding
from .patch import PatchSet


Mode = Literal["review", "audit", "dedup"]


class AgentState(TypedDict, total=False):
    """Shared state across all agent nodes."""

    # --- Input (set once at graph entry) ---
    mode: Mode
    repo_path: str
    target_ref: str            # e.g. "HEAD", "main..feature", or "" for full-repo
    target_description: str    # human description for prompts
    profile_name: str          # which profile is active
    profile_hints: str         # the profile's reasoning hints

    # --- Plan node output ---
    plan: str

    # --- ReAct loop state ---
    messages: Annotated[list, add_messages]  # LLM <-> tool dialogue
    tool_calls_used: int
    tool_calls_budget: int
    iteration: int             # safety counter for the react loop

    # --- Findings (accumulated across react iterations) ---
    findings: Annotated[list[Finding], operator.add]

    # --- Patches proposed by the agent for dedup --fix ---
    patches: Annotated[list[PatchSet], operator.add]

    # --- Reflect node output ---
    summary: str
    systemic_observations: list[str]

    # --- Grounding validator output (post-react) ---
    dropped_findings: Annotated[list[dict], operator.add]

    # --- Bookkeeping ---
    started_at: float
    finished_at: float
    model_used: str
