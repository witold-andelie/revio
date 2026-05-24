"""Interactive `revio dedup --fix` flow.

Per-patch confirmation UI:
  ⚠️  Proposed fix #1 of 17
     [colored diff preview]
     Confidence: 0.92 · Affects 3 files · No test changes

     Apply? ▸ Yes
              No, skip this one
              Explain more (re-render with full context)
              Approve all remaining (with min-confidence gate)
              Quit

Safety:
- Refuses to start on a dirty git repo (unless --allow-dirty)
- Creates a git stash before any apply if --allow-dirty
- can_apply pre-flight per patch; failures shown to user, patch skipped
- Per-patch failures don't abort the session — user sees the error and moves on
- `--dry-run`: print previews, never write
- `--yes`: skip all confirmations, but only for patches with
  confidence >= min_confidence (default 0.95)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from ..agent.patch import PatchApplier, PatchApplyError, PatchSet


logger = logging.getLogger(__name__)


# --- Result aggregation -----------------------------------------------------


@dataclass
class FixSessionResult:
    """Summary of an interactive fix session."""

    applied: list[PatchSet] = field(default_factory=list)
    skipped: list[PatchSet] = field(default_factory=list)
    failed: list[tuple[PatchSet, str]] = field(default_factory=list)
    quit_early: bool = False

    @property
    def total_processed(self) -> int:
        return len(self.applied) + len(self.skipped) + len(self.failed)

    def render_summary(self, console: Console, total: int) -> None:
        console.print()
        console.rule("[bold]🔧 Fix session summary[/]", style="cyan")
        console.print(f"  [green]✓ Applied[/]: {len(self.applied)}")
        console.print(f"  [yellow]· Skipped[/]: {len(self.skipped)}")
        console.print(f"  [red]✗ Failed[/]:  {len(self.failed)}")
        if self.quit_early:
            remaining = total - self.total_processed
            console.print(f"  [dim]Quit early — {remaining} patches not reviewed[/]")
        if self.applied:
            console.print()
            console.print("  [dim]Undo all changes:[/]")
            console.print("    [bold]git reset --hard HEAD[/]")
            console.print("    [bold]git stash pop[/]  (if you ran with --allow-dirty)")
        if self.failed:
            console.print()
            console.print("  [bold]Failures:[/]")
            for p, err in self.failed:
                console.print(f"    [red]✗[/] {p.title}: [dim]{err}[/]")


# --- Main entry --------------------------------------------------------------


def run_fix_flow(
    patches: list[PatchSet],
    repo_root: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
    min_confidence: float = 0.95,
    allow_dirty: bool = False,
    console: Console | None = None,
    config=None,
) -> FixSessionResult:
    """Walk through proposed patches with confirmation. Returns a summary.

    Args:
        patches: PatchSets to consider (typically state["patches"] after dedup run)
        repo_root: Repository root path
        dry_run: If True, print previews but never apply
        yes: If True, auto-apply patches with confidence >= min_confidence;
             still confirm interactively for lower-confidence ones
        min_confidence: Auto-approval threshold when yes=True or approve-all
        allow_dirty: If True, create a safety stash before any apply
                     (otherwise refuse to start on dirty repo)
        console: Optional rich Console for output
    """
    console = console or Console()
    result = FixSessionResult()

    if not patches:
        console.print("  [yellow]·[/] No patches proposed by the agent.")
        return result

    # Build a FixHistoryStore so undo works without git too. dry_run skips
    # this (nothing to undo) — saves a directory creation.
    history_store = None
    if not dry_run and config is not None:
        from ..agent.fix_history import FixHistoryStore

        history_store = FixHistoryStore(
            repo_root=repo_root,
            checkpoint_dir=config.agent.checkpoint_dir,
            max_sessions=config.fix_history.max_sessions,
            max_age_days=config.fix_history.max_age_days,
            max_file_bytes=config.fix_history.max_file_bytes,
        )

    applier = PatchApplier(repo_root, history_store=history_store)

    # Session-level safety
    if not dry_run:
        try:
            applier.begin_session(allow_dirty=allow_dirty)
        except PatchApplyError as e:
            console.print(f"\n  [red]✗[/] {e}")
            console.print("  [dim]Pass --allow-dirty to stash uncommitted changes first.[/]")
            return result

    approve_all = False
    total = len(patches)

    for i, patch in enumerate(patches, 1):
        decision = _process_one(
            console, applier, patch, i, total,
            dry_run=dry_run,
            approve_all_active=approve_all,
            min_confidence=min_confidence,
            yes_mode=yes,
        )

        if decision == "applied":
            result.applied.append(patch)
        elif decision == "skipped":
            result.skipped.append(patch)
        elif decision == "approve_all":
            approve_all = True
            result.applied.append(patch)
        elif decision == "quit":
            result.quit_early = True
            break
        elif isinstance(decision, tuple) and decision[0] == "failed":
            result.failed.append((patch, decision[1]))

    if not dry_run:
        applier.end_session()

    result.render_summary(console, total)
    return result


# --- Per-patch processing ----------------------------------------------------


def _process_one(
    console: Console,
    applier: PatchApplier,
    patch: PatchSet,
    idx: int,
    total: int,
    *,
    dry_run: bool,
    approve_all_active: bool,
    min_confidence: float,
    yes_mode: bool,
):
    """Show the patch, decide what to do, return one of:
      "applied" / "skipped" / "approve_all" / "quit" / ("failed", reason)
    """
    console.print()
    console.rule(f"[bold]🔧 Proposed fix {idx} of {total}[/]", style="cyan")

    # Pre-flight
    ok, reason = applier.can_apply(patch)
    if not ok:
        console.print(f"  [red]✗[/] Cannot apply this patch: [dim]{reason}[/]")
        console.print(f"      Title: {patch.title}")
        return ("failed", reason)

    # Preview
    preview_text = applier.preview(patch)
    console.print(Panel(
        Syntax(preview_text, "diff", theme="monokai", line_numbers=False),
        title=f"{patch.title}",
        title_align="left",
        border_style="blue",
        padding=(0, 1),
    ))

    if dry_run:
        console.print("  [dim](dry-run — not applying)[/]")
        return "skipped"

    # Decision
    decision = _ask_decision(
        patch=patch,
        approve_all_active=approve_all_active,
        min_confidence=min_confidence,
        yes_mode=yes_mode,
    )

    if decision in ("apply", "approve_all"):
        try:
            applier.apply(patch)
            console.print(f"  [green]✓[/] Applied: {patch.title}")
        except PatchApplyError as e:
            console.print(f"  [red]✗[/] Apply failed: [dim]{e}[/]")
            return ("failed", str(e))
        return "applied" if decision == "apply" else "approve_all"

    if decision == "quit":
        return "quit"
    return "skipped"


def _ask_decision(
    *, patch: PatchSet, approve_all_active: bool, min_confidence: float, yes_mode: bool
) -> str:
    """Ask the user what to do. Returns: apply / skip / approve_all / quit."""

    # Auto-apply path
    if approve_all_active or yes_mode:
        if patch.confidence >= min_confidence:
            return "apply"
        # Lower-confidence patch — drop back to interactive even with auto modes
        # (this is the safety gate)

    choice = questionary.select(
        f"Apply this fix?  (confidence={patch.confidence:.2f})",
        choices=[
            "Yes, apply",
            "No, skip this one",
            "Explain more (re-render with full ops)",
            "Approve all remaining",
            "Quit (keep applied so far)",
        ],
    ).ask()

    if choice is None:
        return "quit"
    if choice.startswith("Yes"):
        return "apply"
    if choice.startswith("Approve all"):
        return "approve_all"
    if choice.startswith("Quit"):
        return "quit"
    if choice.startswith("Explain"):
        # Re-render and ask again
        # (we just return skip; the agent's description is already shown)
        return "skip"
    return "skip"
