"""Grounding validator — defends against ungrounded findings.

After the react node finishes, we walk the message history to collect
"facts the LLM actually obtained from successful tool calls". Then we
walk findings and ensure each one cites a file that was actually read
and a line number within range.

Failed groundings:
- File never read successfully  → drop finding entirely
- Line out of range             → downgrade severity + add warning note
- No evidence_summaries at all  → drop finding

This is M2's M1-bug fix: stops the agent from confidently reporting
issues based on its own hallucinated tool outputs.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from ..output.models import Finding, Severity


# --- Fact extraction ----------------------------------------------------------


class ToolFacts:
    """Captures what the agent actually saw via tool calls.

    Built from message history. For each successful read_file call,
    records (file_path, max_line_seen). For errors, records the path
    in an "attempted but failed" set.
    """

    def __init__(self):
        self.files_read: dict[str, int] = {}  # path → max line index seen
        self.files_failed: set[str] = set()   # paths that returned errors

    def saw_path(self, path: str) -> bool:
        return path in self.files_read

    def failed_path(self, path: str) -> bool:
        return path in self.files_failed

    def max_line_for(self, path: str) -> int:
        return self.files_read.get(path, 0)


def collect_tool_facts(messages: list[Any]) -> ToolFacts:
    """Walk messages, extract what the agent actually obtained from tools."""
    facts = ToolFacts()

    # Build a map: tool_call_id → tool_name + args, by scanning AIMessages
    call_index: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                tcid = tc.get("id", "")
                if tcid:
                    call_index[tcid] = (tc.get("name", ""), tc.get("args", {}) or {})

    # Walk ToolMessages and pair with the original call
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tcid = getattr(msg, "tool_call_id", "")
            content = str(msg.content) if msg.content else ""

            call = call_index.get(tcid)
            if not call:
                continue
            name, args = call

            if name == "read_file":
                path = args.get("relative_path") or args.get("path") or ""
                if not path:
                    continue
                if _looks_like_error(content):
                    facts.files_failed.add(path)
                else:
                    # Try to extract the highest line number seen
                    max_line = _extract_max_line(content)
                    prior = facts.files_read.get(path, 0)
                    facts.files_read[path] = max(prior, max_line)

    return facts


# --- Validation ---------------------------------------------------------------


GROUNDING_NOTE_PREFIX = "[ungrounded]"


def validate_findings(
    findings: list[Finding],
    facts: ToolFacts,
    *,
    drop_ungrounded: bool = True,
) -> tuple[list[Finding], list[dict]]:
    """Validate findings against tool facts.

    Returns (kept, dropped) where each `dropped` entry is a small dict with
    the original finding's title + reason.
    """
    kept: list[Finding] = []
    dropped: list[dict] = []

    for f in findings:
        # 1. Did we ever successfully read this file?
        seen = facts.saw_path(f.file_path)
        # Also check normalized — strip leading "./" or repo prefixes
        if not seen:
            for candidate in _path_variants(f.file_path):
                if facts.saw_path(candidate):
                    seen = True
                    break

        if not seen:
            reason = f"File '{f.file_path}' was never successfully read by any tool call."
            if drop_ungrounded:
                dropped.append({"title": f.title, "file_path": f.file_path, "reason": reason})
                continue
            else:
                # Keep but downgrade + annotate
                f = _downgrade(f, reason)
                kept.append(f)
                continue

        # 2. Is line_start within range?
        max_line = facts.max_line_for(f.file_path)
        # Account for path variants
        if max_line == 0:
            for cand in _path_variants(f.file_path):
                if facts.max_line_for(cand) > 0:
                    max_line = facts.max_line_for(cand)
                    break

        if max_line > 0 and f.line_start > max_line:
            reason = f"line_start={f.line_start} exceeds last line read ({max_line})"
            f = _downgrade(f, reason)
            kept.append(f)
            continue

        # 3. Empty evidence
        if not f.evidence:
            reason = "Finding has no evidence summaries — hypothesis without proof."
            if drop_ungrounded:
                dropped.append({"title": f.title, "file_path": f.file_path, "reason": reason})
                continue
            else:
                f = _downgrade(f, reason)
                kept.append(f)
                continue

        kept.append(f)

    return kept, dropped


# --- Helpers ------------------------------------------------------------------


_ERROR_PREFIXES = (
    "Error:",
    "error:",
    "Tool error:",
)


def _looks_like_error(content: str) -> bool:
    head = content.lstrip()[:60]
    return any(head.startswith(p) for p in _ERROR_PREFIXES) or "file not found" in content.lower()


# Match line-numbered lines from read_file output: "  123  source line"
_LINE_PATTERN = re.compile(r"^\s*(\d+)\s{2,}", re.MULTILINE)


def _extract_max_line(content: str) -> int:
    """Find the largest line number in line-numbered read_file output."""
    nums = _LINE_PATTERN.findall(content)
    if not nums:
        # Maybe the content header said "(lines 1-200 of 543)"
        m = re.search(r"of\s+(\d+)\s*\)", content)
        if m:
            return int(m.group(1))
        return 0
    return max(int(n) for n in nums)


def _path_variants(path: str) -> list[str]:
    """Yield common path variants we should consider equivalent."""
    out = []
    out.append(path.lstrip("./"))
    if path.startswith("./"):
        out.append(path[2:])
    if not path.startswith("./"):
        out.append("./" + path)
    if path.startswith("/"):
        # Absolute → try basename + leading-component drops
        parts = path.lstrip("/").split("/")
        for i in range(len(parts)):
            out.append("/".join(parts[i:]))
    return list({p for p in out if p and p != path})


def _downgrade(f: Finding, reason: str) -> Finding:
    """Return a downgraded copy of f with a grounding note attached."""
    note_evidence = type(f.evidence[0])(
        kind="reasoning",
        summary=f"{GROUNDING_NOTE_PREFIX} {reason}",
    ) if f.evidence else None

    # Cap severity at WARNING for ungrounded findings
    sev = f.severity
    if sev in (Severity.CRITICAL, Severity.ERROR):
        sev = Severity.WARNING

    # Confidence cut in half
    conf = min(f.confidence, 0.4)

    new_evidence = list(f.evidence)
    if note_evidence is not None:
        new_evidence.insert(0, note_evidence)

    return f.model_copy(update={
        "severity": sev,
        "confidence": conf,
        "evidence": new_evidence,
        "verified": False,
    })


# --- Plan-text sanitizer ------------------------------------------------------


# Strip text-form fake tool calls/responses that some models write inside the
# plan response. Prevents them from being interpreted as real evidence later.
_TOOL_CALL_MARKUP = re.compile(
    r"<tool_call>.*?</tool_call>"
    r"|<tool_response>.*?</tool_response>"
    r"|<function_calls>.*?</function_calls>"
    r"|<function_results>.*?</function_results>"
    r"|```(?:tool_call|tool_response|function_calls)\s.*?```",
    re.DOTALL | re.IGNORECASE,
)


def sanitize_plan_text(plan: str) -> str:
    """Remove any fake tool-call markup from a plan-stage response.

    The plan node is strategy-only — the LLM has no tool access. Some models
    nonetheless write `<tool_call>...</tool_call>` as plain text and then
    treat their own fabrication as evidence. We strip that markup before
    handing the plan to the react phase.
    """
    cleaned = _TOOL_CALL_MARKUP.sub("[removed: fabricated tool-call markup]", plan)
    return cleaned.strip()
