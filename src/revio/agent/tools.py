"""Universal agent tools (available in every profile).

Includes:
- list_files, read_file        : repo exploration
- report_finding               : structured finding emission via Command state update
- search_guidelines            : RAG over company/client coding standards

Profile-specific tools (run_oxlint, get_function_at, ...) live in js_tools.py.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool
from langgraph.types import Command

from ..output.models import (
    Evidence,
    Finding,
    ReviewCategory,
    Severity,
)
from .tool_context import ToolContext


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


# --- search_guidelines --------------------------------------------------------


# --- load_skill ---------------------------------------------------------------


def make_load_skill_tool(ctx: ToolContext):
    """Return the load_skill tool — agent fetches a skill's full body on demand.

    The plan-stage system prompt lists every skill's NAME + DESCRIPTION but not
    its body (Anthropic-style progressive disclosure). When the agent decides
    a skill is relevant, it calls load_skill(name) to pull the full content.
    """

    @tool
    def load_skill(name: str) -> str:
        """Load the full content of a skill (procedural knowledge for a scenario).

        Call this when the skill's catalog entry (in the system prompt) looks
        relevant to what you're investigating. The full body contains examples,
        anti-patterns, and concrete review checklists.

        Args:
            name: The skill identifier (from the catalog in the system prompt).

        Returns:
            The markdown body of the skill, or an error message.
        """
        skill = ctx.skills_registry.get(name)
        if skill is None:
            available = ", ".join(sorted(ctx.skills_registry.skills.keys())) or "(none)"
            return (
                f"Error: no skill named {name!r}. "
                f"Available skills: {available}"
            )
        body = skill.load_body()
        if not body.strip():
            return f"Skill {name!r} has an empty body."
        return f"# Skill: {name}\n## {skill.description}\n\n{body}"

    return load_skill


def make_search_guidelines_tool(ctx: ToolContext):
    """Build the search_guidelines tool bound to a session's RAG retriever.

    Returns a no-op tool if no guidelines are indexed (the agent will see
    the message and skip RAG queries).
    """

    @tool
    def search_guidelines(query: str, k: int = 5) -> str:
        """Search the company's coding guidelines / policies via semantic search.

        Use this BEFORE flagging style/architecture/security findings to verify
        whether the project has a documented policy on the pattern you're
        questioning. Citing a specific guideline section makes findings
        actionable and avoids opinionated false positives.

        Args:
            query: Natural-language query. Examples:
              - "SQL injection prevention"
              - "naming conventions for React components"
              - "logging policy for sensitive data"
              - "approved cryptographic algorithms"
            k: Number of results to return (default 5, max 20).

        Returns:
            Top-k matching guideline chunks with source file and section.
            If no guidelines are indexed, returns a clear message.
        """
        retriever = ctx.rag
        if retriever is None:
            return (
                "No guidelines indexed for this repository. "
                "The user can add guidelines via `revio guidelines add <path>`. "
                "Proceed without policy-based evidence — your findings will rely on "
                "general best practices alone."
            )

        k = max(1, min(20, k))
        results = retriever.search_with_scores(query, k=k)
        if not results:
            return f"(no guideline chunks matched query {query!r})"

        lines = [f"# Guideline search: {query!r} ({len(results)} hits)"]
        for doc, score in results:
            source = Path(doc.metadata.get("source", "?")).name
            section = doc.metadata.get("section_title", "")
            page = doc.metadata.get("page_number")
            location = f"{source}"
            if section:
                location += f" / {section}"
            if page:
                location += f" (p.{page})"
            lines.append(f"\n[{location}] (relevance={score:.2f})")
            # Indent the chunk body so it's visually separated
            body = doc.page_content.strip()
            if len(body) > 600:
                body = body[:600] + "..."
            for ln in body.splitlines():
                lines.append(f"  {ln}")
        lines.append(
            "\n# How to use these results:\n"
            "  Cite the specific guideline (filename + section title) as one of your "
            "evidence_summaries when calling report_finding. Example:\n"
            "    'security_checklist.md / SQL Injection Prevention: requires parameterized queries'"
        )
        return "\n".join(lines)

    return search_guidelines
