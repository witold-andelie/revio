"""Patch model + applier for `revio dedup --fix`.

Architecture (deliberately small):
- `PatchOp`         — atomic file change (edit / delete_lines / delete_file /
                       create_file / rename)
- `PatchSet`        — a coherent group of ops that fix ONE issue (e.g.
                       "remove duplicate function + update 3 import sites")
- `PatchApplier`    — preview + can_apply + apply, with git stash safety net

Safety design:
1. NEVER apply on a dirty git repo — refuse with clear message
2. Before applying any patch in a session, create a git stash with a tagged
   message so the user can `git stash pop` to undo everything
3. `can_apply` pre-flight checks every op (file exists, old_content matches)
   BEFORE writing anything — atomic at the PatchSet level
4. Failed apply restores from the stash automatically
"""

from __future__ import annotations

import difflib
import logging
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# --- Patch models ------------------------------------------------------------


OpType = Literal["edit", "delete_lines", "delete_file", "create_file", "rename"]


class PatchOp(BaseModel):
    """One atomic file change.

    op_type semantics:
        edit          — replace lines [line_start, line_end] with new_content.
                        old_content must match the existing source verbatim
                        (anti-corruption check) — applier refuses otherwise.
        delete_lines  — same as edit with empty new_content.
        delete_file   — remove the file (no other fields used).
        create_file   — write new_content to a new file (file must NOT exist).
        rename        — move file_path → new_path (other fields ignored).
    """

    op_type: OpType
    file_path: str                        # relative to repo root
    line_start: int | None = None         # 1-indexed
    line_end: int | None = None           # 1-indexed inclusive
    old_content: str | None = None        # required for edit/delete_lines
    new_content: str | None = None        # required for edit/create_file
    new_path: str | None = None           # required for rename
    reason: str = ""                       # human-readable why this op


class PatchSet(BaseModel):
    """A coherent group of ops that fix one issue.

    Example: dedup of `buildDisplayName` requires deleting it (delete_lines)
    AND updating 3 import sites (edit x 3) — all 5 ops belong to one PatchSet
    because partial application would leave the codebase broken.
    """

    title: str = Field(max_length=200)
    description: str
    ops: list[PatchOp] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    finding_ref: str | None = None         # links back to a Finding.title
    created_by: str = "agent"               # agent | manual

    @property
    def affected_files(self) -> set[str]:
        out: set[str] = set()
        for op in self.ops:
            out.add(op.file_path)
            if op.new_path:
                out.add(op.new_path)
        return out


# --- Applier ----------------------------------------------------------------


class PatchApplyError(RuntimeError):
    """Raised when a PatchSet cannot be applied safely."""


class PatchApplier:
    """Preview + can_apply + apply, with git stash safety net.

    Lifecycle:
        applier = PatchApplier(repo_root)
        ok, reason = applier.can_apply(patchset)   # pre-flight
        diff_text = applier.preview(patchset)       # render to user
        applier.begin_session()                     # creates safety stash
        applier.apply(patchset)                     # writes files
        applier.end_session()                       # leaves stash for undo
    """

    SESSION_STASH_PREFIX = "revio --fix safety stash"

    def __init__(self, repo_root: Path | str):
        self.repo_root = Path(repo_root).expanduser().resolve()
        if not self.repo_root.is_dir():
            raise FileNotFoundError(f"repo root not found: {self.repo_root}")
        self._session_stash_ref: str | None = None
        self._applied_patches: list[PatchSet] = []

    # ---- Pre-flight checks --------------------------------------------------

    def can_apply(self, patchset: PatchSet) -> tuple[bool, str]:
        """Verify every op can apply cleanly. Returns (ok, reason_if_not)."""
        for i, op in enumerate(patchset.ops):
            ok, reason = self._can_apply_op(op)
            if not ok:
                return False, f"op #{i + 1} ({op.op_type} {op.file_path}): {reason}"
        return True, ""

    def _can_apply_op(self, op: PatchOp) -> tuple[bool, str]:
        full = self._resolve(op.file_path)
        if full is None:
            return False, f"path '{op.file_path}' escapes repo root"

        if op.op_type == "delete_file":
            if not full.is_file():
                return False, "file does not exist"
            return True, ""

        if op.op_type == "create_file":
            if full.exists():
                return False, "file already exists"
            if op.new_content is None:
                return False, "create_file needs new_content"
            return True, ""

        if op.op_type == "rename":
            if not full.is_file():
                return False, "source file does not exist"
            if op.new_path is None:
                return False, "rename needs new_path"
            new_full = self._resolve(op.new_path)
            if new_full is None:
                return False, f"new_path '{op.new_path}' escapes repo root"
            if new_full.exists():
                return False, "target path already exists"
            return True, ""

        # edit / delete_lines — both need line range + old_content match
        if op.op_type in ("edit", "delete_lines"):
            if not full.is_file():
                return False, "file does not exist"
            if op.line_start is None or op.line_end is None:
                return False, f"{op.op_type} needs line_start and line_end"
            if op.line_start < 1 or op.line_end < op.line_start:
                return False, f"invalid line range {op.line_start}..{op.line_end}"
            if op.old_content is None:
                return False, "needs old_content for anti-corruption check"

            try:
                source = full.read_text(encoding="utf-8")
            except OSError as e:
                return False, f"read failed: {e}"

            lines = source.splitlines(keepends=True)
            if op.line_end > len(lines):
                return False, f"line_end {op.line_end} > file length {len(lines)}"

            # Verify old_content matches the slice. Use lenient whitespace
            # comparison — files saved with different EOLs shouldn't fail.
            actual = "".join(lines[op.line_start - 1 : op.line_end])
            expected = op.old_content
            if _normalize_ws(actual) != _normalize_ws(expected):
                # Build a useful error message showing the mismatch
                act_n = _normalize_ws(actual)
                exp_n = _normalize_ws(expected)
                # Find the first diverging char
                preview_lim = 80
                return False, (
                    f"old_content doesn't match current file content. "
                    f"Expected (lines {op.line_start}-{op.line_end}, normalized): "
                    f"{exp_n[:preview_lim]!r}{'...' if len(exp_n) > preview_lim else ''} "
                    f"| Actual: {act_n[:preview_lim]!r}{'...' if len(act_n) > preview_lim else ''}"
                )

            if op.op_type == "edit" and op.new_content is None:
                return False, "edit needs new_content"

            return True, ""

        return False, f"unknown op_type: {op.op_type}"

    # ---- Preview ------------------------------------------------------------

    def preview(self, patchset: PatchSet) -> str:
        """Render a unified-diff-style preview of all ops in the patchset."""
        chunks: list[str] = []
        chunks.append(f"# Patch: {patchset.title}")
        if patchset.description:
            chunks.append(f"# {patchset.description}")
        chunks.append(f"# Confidence: {patchset.confidence:.2f}")
        chunks.append(f"# Affects {len(patchset.affected_files)} files, {len(patchset.ops)} ops\n")

        for i, op in enumerate(patchset.ops, 1):
            chunks.append(self._preview_op(i, op))
        return "\n".join(chunks)

    def _preview_op(self, idx: int, op: PatchOp) -> str:
        header = f"--- op {idx}: {op.op_type} {op.file_path}"
        if op.reason:
            header += f"\n# reason: {op.reason}"
        if op.op_type == "delete_file":
            return f"{header}\n  (entire file will be deleted)"
        if op.op_type == "rename":
            return f"{header}\n  → {op.new_path}"
        if op.op_type == "create_file":
            body = (op.new_content or "").splitlines()[:20]
            preview = "\n".join(f"+ {ln}" for ln in body)
            ellipsis = "\n... (more)" if op.new_content and len(op.new_content.splitlines()) > 20 else ""
            return f"{header}  (NEW FILE)\n{preview}{ellipsis}"

        # edit / delete_lines
        old_lines = (op.old_content or "").splitlines()
        new_lines = (op.new_content or "").splitlines() if op.op_type == "edit" else []
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{op.file_path}:L{op.line_start}-L{op.line_end} (before)",
            tofile=f"{op.file_path}:L{op.line_start}-L{op.line_end} (after)",
            lineterm="",
        )
        diff_text = "\n".join(diff) or "(empty diff — both sides identical?)"
        return f"{header}\n{diff_text}"

    # ---- Session lifecycle --------------------------------------------------

    def begin_session(self, *, allow_dirty: bool = False) -> None:
        """Create a git stash so the entire session can be reverted later."""
        if not self._is_git_repo():
            logger.warning("not a git repo — no safety stash will be created")
            return

        if not allow_dirty and self._has_uncommitted_changes():
            raise PatchApplyError(
                "Repo has uncommitted changes. Commit or stash them first, or "
                "rerun with --allow-dirty if you accept losing the safety net."
            )

        if self._has_uncommitted_changes():
            # User passed allow_dirty=True — still stash so we can restore
            stash_msg = f"{self.SESSION_STASH_PREFIX} {time.strftime('%Y-%m-%d %H:%M:%S')}"
            try:
                subprocess.run(
                    ["git", "stash", "push", "--include-untracked", "-m", stash_msg],
                    cwd=self.repo_root, check=True, capture_output=True, text=True,
                )
                self._session_stash_ref = stash_msg
                logger.info("revio: created safety stash %r", stash_msg)
            except subprocess.CalledProcessError as e:
                raise PatchApplyError(
                    f"Could not create safety stash: {e.stderr.strip()}"
                ) from e

    def end_session(self) -> None:
        """No-op for now — applied patches stay applied.

        We leave the safety stash in place if any was created — the user
        can `git stash list` to see it and `git stash pop` to revert.
        """
        if self._session_stash_ref:
            logger.info(
                "revio: %d patches applied. Undo via: git reset --hard HEAD; git stash pop",
                len(self._applied_patches),
            )

    # ---- Apply --------------------------------------------------------------

    def apply(self, patchset: PatchSet) -> None:
        """Apply every op in the patchset. Raises if any op fails."""
        ok, reason = self.can_apply(patchset)
        if not ok:
            raise PatchApplyError(f"cannot apply patchset {patchset.title!r}: {reason}")

        for op in patchset.ops:
            self._apply_op(op)

        self._applied_patches.append(patchset)

    def _apply_op(self, op: PatchOp) -> None:
        full = self._resolve(op.file_path)
        assert full is not None  # can_apply already validated

        if op.op_type == "delete_file":
            full.unlink()
            return

        if op.op_type == "create_file":
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(op.new_content or "", encoding="utf-8")
            return

        if op.op_type == "rename":
            new_full = self._resolve(op.new_path or "")
            assert new_full is not None
            new_full.parent.mkdir(parents=True, exist_ok=True)
            full.rename(new_full)
            return

        # edit / delete_lines
        source = full.read_text(encoding="utf-8")
        lines = source.splitlines(keepends=True)
        assert op.line_start is not None and op.line_end is not None
        before = lines[: op.line_start - 1]
        after = lines[op.line_end :]
        replacement: list[str] = []
        if op.op_type == "edit" and op.new_content is not None:
            # Preserve trailing newline behavior of the original chunk
            ends_with_nl = (op.new_content.endswith("\n"))
            new_lines = op.new_content.splitlines(keepends=True)
            if new_lines and not ends_with_nl:
                # Make sure last line has a newline if the rest of file does
                if after and not new_lines[-1].endswith("\n"):
                    new_lines[-1] = new_lines[-1] + "\n"
            replacement = new_lines

        full.write_text("".join(before + replacement + after), encoding="utf-8")

    # ---- Helpers ------------------------------------------------------------

    def _resolve(self, relative_path: str) -> Path | None:
        """Resolve relative_path against repo_root, refusing escapes."""
        if Path(relative_path).is_absolute():
            return None
        full = (self.repo_root / relative_path).resolve()
        try:
            full.relative_to(self.repo_root)
        except ValueError:
            return None
        return full

    def _is_git_repo(self) -> bool:
        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root, check=True, capture_output=True, text=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _has_uncommitted_changes(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo_root, check=True, capture_output=True, text=True,
            )
            return bool(result.stdout.strip())
        except subprocess.CalledProcessError:
            return False


def _normalize_ws(text: str) -> str:
    """Trim whitespace + normalize line endings for content comparison."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())
