"""Agent tools.

M1: only two tools.
- read_file : let the agent fetch source on demand (so it pulls only what it needs)
- report_finding : let the agent emit structured findings via state update

More tools (grep, get_call_sites, run_oxlint, ...) arrive in M2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from ..output.models import (
    Evidence,
    Finding,
    ReviewCategory,
    Severity,
)


# --- list_files ---------------------------------------------------------------


# Directories we never list (noise, not source code)
_LIST_IGNORE_DIRS = {
    "node_modules", "__pycache__", ".git", "venv", ".venv", "env",
    "dist", "build", "target", ".next", ".nuxt", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "coverage", "htmlcov",
    ".idea", ".vscode", ".cache", ".terraform",
}


def make_list_files_tool(repo_root: Path):
    """Build a list_files tool bound to a specific repo root."""
    repo_root = repo_root.resolve()

    @tool
    def list_files(subdir: str = ".", max_files: int = 200) -> str:
        """List files in the repository (or a subdirectory).

        Call this FIRST before reading files — do not guess filenames.

        Args:
            subdir: Subdirectory relative to repo root. Default "." for root.
            max_files: Truncate output if more files exist (default 200).

        Returns:
            One file path per line, relative to repo root.
        """
        if Path(subdir).is_absolute():
            return "Error: absolute paths not allowed."

        base = (repo_root / subdir).resolve()
        try:
            base.relative_to(repo_root)
        except ValueError:
            return f"Error: '{subdir}' is outside the repository."

        if not base.is_dir():
            return f"Error: not a directory: {subdir}"

        files: list[str] = []
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            # Skip ignored dirs anywhere in the path
            rel_parts = p.relative_to(repo_root).parts
            if any(part in _LIST_IGNORE_DIRS for part in rel_parts[:-1]):
                continue
            files.append(str(p.relative_to(repo_root)))
            if len(files) >= max_files:
                break

        files.sort()
        if not files:
            return f"(no files in {subdir})"

        header = f"# Files in {subdir} ({len(files)}{'+' if len(files) >= max_files else ''} total)"
        return header + "\n" + "\n".join(files)

    return list_files


# --- read_file ----------------------------------------------------------------


def make_read_file_tool(repo_root: Path):
    """Build a read_file tool bound to a specific repo root.

    The closure captures repo_root so the LLM can't escape it (path traversal
    is checked on every call).
    """
    repo_root = repo_root.resolve()

    @tool
    def read_file(
        relative_path: str,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> str:
        """Read a file from the repository (line-numbered).

        Args:
            relative_path: Path relative to the repo root. Absolute paths are rejected.
            start_line: 1-indexed line to start from (default 1).
            max_lines: Truncate output if longer (default 200).

        Returns:
            Numbered source lines, or an error message.
        """
        # Path validation
        if Path(relative_path).is_absolute():
            return f"Error: absolute paths not allowed. Use a path relative to repo root."

        full_path = (repo_root / relative_path).resolve()
        # Reject paths that escape repo_root
        try:
            full_path.relative_to(repo_root)
        except ValueError:
            return f"Error: path '{relative_path}' is outside the repository."

        if not full_path.is_file():
            return f"Error: file not found: {relative_path}"

        try:
            text = full_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            return f"Error reading file: {e}"

        all_lines = text.splitlines()
        total = len(all_lines)

        if start_line < 1:
            start_line = 1
        if start_line > total:
            return f"Error: start_line {start_line} > file length {total}"

        end = min(start_line + max_lines - 1, total)
        slice_lines = all_lines[start_line - 1 : end]

        numbered = "\n".join(
            f"{start_line + i:5d}  {line}"
            for i, line in enumerate(slice_lines)
        )

        if end < total:
            numbered += f"\n... ({total - end} more lines)"

        return f"# {relative_path} (lines {start_line}-{end} of {total})\n{numbered}"

    return read_file


# --- report_finding -----------------------------------------------------------


@tool
def report_finding(
    file_path: str,
    line_start: int,
    severity: str,
    category: str,
    title: str,
    hypothesis: str,
    evidence_summaries: list[str],
    suggestion: str = "",
    counter_considered: str = "",
    confidence: float = 0.8,
    line_end: int | None = None,
) -> Command:
    """Record a finding from your investigation.

    Call this when you've confirmed an issue. Use the hypothesis-evidence model:
    state what you think is wrong (hypothesis), then list each piece of evidence
    you gathered (from tool calls or reasoning) as a short summary.

    Args:
        file_path: File where the issue is, relative to repo root.
        line_start: Starting line number.
        severity: One of: info, warning, error, critical.
        category: One of: code_style, potential_bug, security, architecture,
                  readability, convention, performance, redundancy.
        title: Short title (max 10 words).
        hypothesis: One sentence stating what you claim is wrong.
        evidence_summaries: List of one-line evidence statements (3+ recommended).
        suggestion: How to fix it (optional, include code if useful).
        counter_considered: Alternative explanations you ruled out (optional).
        confidence: 0.0 - 1.0, how sure are you (default 0.8).
        line_end: End line if multi-line issue (optional).
    """
    try:
        sev = Severity(severity.lower())
    except ValueError:
        sev = Severity.WARNING

    try:
        cat = ReviewCategory(category.lower())
    except ValueError:
        cat = ReviewCategory.POTENTIAL_BUG

    finding = Finding(
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        severity=sev,
        category=cat,
        title=title,
        hypothesis=hypothesis,
        evidence=[
            Evidence(kind="reasoning", summary=s)
            for s in evidence_summaries
        ],
        counter_considered=counter_considered or None,
        confidence=max(0.0, min(1.0, confidence)),
        verified=False,
        suggestion=suggestion or None,
        detected_by="agent",
    )

    # Use a Command to update state (findings reducer concatenates)
    return Command(update={"findings": [finding]})
