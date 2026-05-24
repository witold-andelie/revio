"""Fix history: snapshot-based, multi-step undo for `revio dedup --fix`.

# Why this exists

`PatchApplier` previously relied on a single git stash per session as
the only undo path. Two failure modes that left users stranded:

1. **Not in a git repo** → no safety at all (only a warning logged).
2. **Multiple `--fix` runs accumulate** → git stash stack gets a bunch
   of identically-titled entries with no clear mapping back to which
   files each one touched.

This module records every `--fix` session under
`~/.cache/revio/<repo_hash>_fix_history/<session_id>/` with:

  manifest.json    — session metadata (timestamp, files, patchset titles)
  applied.json     — the exact PatchSet that was applied (for re-do later)
  snapshots/<relpath>  — full file content BEFORE the patch was applied

Undo restores from snapshots — no git needed, no reverse-patch math.
Files that were created by the patch are deleted on undo (their absence
is the snapshot).

# Auto-rotation

Two caps enforced via `cleanup()`:

  max_sessions  : default 50 → oldest deleted when exceeded
  max_age_days  : default 30 → time-based purge on every begin_session

Files larger than `max_file_bytes` (default 1 MiB) are NOT snapshotted;
the manifest records `oversized` for that path and undo will warn the
user and skip restoring it.

# Layout invariants (don't break these)

- Session IDs are sortable as strings (ISO timestamp + short hash). So
  ascending sort = chronological order.
- Snapshot paths inside `snapshots/` are kept verbatim relative to repo
  root, including subdirectories. We use Path.mkdir(parents=True) to
  reconstruct on restore.
- `manifest.json` is the authoritative listing. If snapshots/ has stale
  files not listed in the manifest, they're ignored on undo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .patch import PatchOp, PatchSet


logger = logging.getLogger(__name__)


# --- Data shapes -------------------------------------------------------------


@dataclass
class FileSnapshot:
    """One file's pre-apply state."""

    relpath: str               # path relative to repo root
    existed: bool              # was the file present before the patch?
    oversized: bool = False    # skipped due to max_file_bytes — undo will warn


@dataclass
class FixSession:
    """Materialized view of one fix session on disk."""

    session_id: str
    started_at: float                    # unix epoch
    started_at_str: str                  # human-readable
    repo_root: str
    patchset_titles: list[str]
    files: list[FileSnapshot]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def applied_ops_count(self) -> int:
        return self._applied_ops_count  # set when read from disk

    _applied_ops_count: int = field(default=0, repr=False)


# --- Path helpers ------------------------------------------------------------


def _repo_hash(repo_root: Path) -> str:
    return hashlib.sha1(str(repo_root).encode()).hexdigest()[:12]


def _history_root(checkpoint_dir: Path, repo_root: Path) -> Path:
    """Where this repo's fix history lives."""
    return checkpoint_dir / f"{_repo_hash(repo_root)}_fix_history"


def _make_session_id() -> str:
    """ISO timestamp with microseconds + 4-char random suffix.

    Sortable as string = chronological. The microsecond field is the
    real ordering source; the random suffix only breaks ties when two
    sessions land in the same microsecond (rare).
    """
    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(now))
    micros = int((now - int(now)) * 1_000_000)
    suffix = uuid.uuid4().hex[:4]
    return f"{ts}.{micros:06d}_{suffix}"


# --- The store ---------------------------------------------------------------


class FixHistoryStore:
    """Disk-backed log of fix sessions for one repo.

    Single instance per `--fix` invocation; PatchApplier holds it for
    the session lifetime, calls `snapshot_files()` before each apply, then
    `finalize()` once after all patches applied to write the manifest.
    """

    def __init__(
        self,
        repo_root: Path | str,
        checkpoint_dir: Path | str,
        *,
        max_sessions: int = 50,
        max_age_days: int = 30,
        max_file_bytes: int = 1_048_576,
    ):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.checkpoint_dir = Path(checkpoint_dir).expanduser()
        self.history_root = _history_root(self.checkpoint_dir, self.repo_root)
        self.max_sessions = max_sessions
        self.max_age_days = max_age_days
        self.max_file_bytes = max_file_bytes

        # Session state — populated by begin_session()
        self.session_id: str | None = None
        self._snapshots: dict[str, FileSnapshot] = {}
        self._applied: list[PatchSet] = []
        self._started_at: float = 0.0

    # ---- Session lifecycle --------------------------------------------------

    def begin_session(self) -> str:
        """Start a new session; returns its ID. Also runs cleanup."""
        self.session_id = _make_session_id()
        self._snapshots.clear()
        self._applied.clear()
        self._started_at = time.time()
        self.history_root.mkdir(parents=True, exist_ok=True)
        self._session_dir().mkdir(parents=True, exist_ok=True)
        (self._session_dir() / "snapshots").mkdir(parents=True, exist_ok=True)
        # Cleanup older entries opportunistically — cheap (os.stat only)
        try:
            self.cleanup()
        except Exception as e:
            logger.warning("fix_history cleanup failed: %s", e)
        return self.session_id

    def snapshot_files(self, patchset: PatchSet) -> None:
        """Snapshot files affected by `patchset` BEFORE applying it.

        Idempotent per file — once snapshotted in this session, we don't
        re-snapshot (the first snapshot is the pre-session state, which
        is what undo needs).
        """
        if self.session_id is None:
            raise RuntimeError("snapshot_files() called outside a session")
        for relpath in patchset.affected_files:
            if relpath in self._snapshots:
                continue
            full = self.repo_root / relpath
            snap = self._snapshot_one(relpath, full)
            self._snapshots[relpath] = snap

    def add_applied(self, patchset: PatchSet) -> None:
        """Record a successfully-applied PatchSet in this session."""
        self._applied.append(patchset)

    def finalize(self) -> None:
        """Write manifest.json + applied.json once all patches applied.

        If no patches were applied (every can_apply failed, dry-run, etc.),
        the session directory is removed to avoid leaving empty entries.
        """
        if self.session_id is None:
            return
        if not self._applied:
            # Nothing was applied — clean up the empty session dir
            try:
                shutil.rmtree(self._session_dir(), ignore_errors=True)
            finally:
                self.session_id = None
            return

        manifest = {
            "session_id": self.session_id,
            "started_at": self._started_at,
            "started_at_str": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self._started_at)
            ),
            "repo_root": str(self.repo_root),
            "patchset_titles": [ps.title for ps in self._applied],
            "applied_ops_count": sum(len(ps.ops) for ps in self._applied),
            "files": [
                {
                    "relpath": s.relpath,
                    "existed": s.existed,
                    "oversized": s.oversized,
                }
                for s in self._snapshots.values()
            ],
        }
        (self._session_dir() / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        applied_dump = {
            "patches": [ps.model_dump(mode="json") for ps in self._applied],
        }
        (self._session_dir() / "applied.json").write_text(
            json.dumps(applied_dump, indent=2, default=str), encoding="utf-8"
        )

        logger.info(
            "fix_history: recorded session %s (%d files, %d ops)",
            self.session_id, len(self._snapshots), manifest["applied_ops_count"],
        )

    # ---- Listing / inspection -----------------------------------------------

    def list_sessions(self, limit: int | None = 10) -> list[FixSession]:
        """Return sessions newest-first. None limit = all."""
        if not self.history_root.is_dir():
            return []
        dirs = sorted(
            (d for d in self.history_root.iterdir() if d.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        if limit is not None:
            dirs = dirs[:limit]
        out: list[FixSession] = []
        for d in dirs:
            sess = self._read_session(d)
            if sess is not None:
                out.append(sess)
        return out

    def get_session(self, session_id: str) -> FixSession | None:
        d = self.history_root / session_id
        if not d.is_dir():
            return None
        return self._read_session(d)

    # ---- Undo ---------------------------------------------------------------

    def undo_session(self, session_id: str) -> tuple[int, list[str]]:
        """Restore files snapshotted in the given session.

        Returns (restored_count, warnings). Warnings cover oversized files
        (no snapshot stored) and any IO failures we skipped past.

        The undo itself is recorded as a NEW session so you can re-do it
        (or undo the undo) later. The new session's snapshots are the
        CURRENT contents — i.e. the post-fix state — so undoing the undo
        re-applies the original fix.
        """
        sess = self.get_session(session_id)
        if sess is None:
            raise FileNotFoundError(f"no such session: {session_id}")

        # Step 1: snapshot current state into a new "undo" session so it's reversible
        undo_session_id = self.begin_session()
        # Forge a fake patchset listing affected files for snapshotting
        fake_files = [s.relpath for s in sess.files]
        if fake_files:
            # We can't construct a real PatchSet easily; just snapshot directly
            for relpath in fake_files:
                full = self.repo_root / relpath
                snap = self._snapshot_one(relpath, full)
                self._snapshots[relpath] = snap

        # Step 2: restore each file
        restored = 0
        warnings: list[str] = []
        old_session_dir = self.history_root / session_id

        for snap in sess.files:
            target = self.repo_root / snap.relpath
            try:
                if snap.oversized:
                    warnings.append(
                        f"skipped {snap.relpath}: was too large at fix time "
                        f"(no snapshot stored)"
                    )
                    continue
                if not snap.existed:
                    # File was created by the fix → delete it to undo
                    if target.is_file():
                        target.unlink()
                        restored += 1
                    continue
                # Restore from snapshot
                snap_path = old_session_dir / "snapshots" / snap.relpath
                if not snap_path.is_file():
                    warnings.append(
                        f"snapshot missing on disk for {snap.relpath} "
                        f"(undo entry corrupted)"
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap_path, target)
                restored += 1
            except OSError as e:
                warnings.append(f"failed to restore {snap.relpath}: {e}")

        # Step 3: finalize the undo session with a synthetic PatchSet entry so
        # manifest non-empty and `revio fix history` shows the undo too
        synthetic = PatchSet(
            title=f"undo of {session_id}",
            description=f"reverted {restored} files from session {session_id}",
            ops=[],
            confidence=1.0,
        )
        self._applied.append(synthetic)
        self.finalize()

        return restored, warnings

    # ---- Cleanup ------------------------------------------------------------

    def cleanup(self) -> None:
        """Enforce max_sessions + max_age_days."""
        if not self.history_root.is_dir():
            return

        all_dirs = sorted(
            (d for d in self.history_root.iterdir() if d.is_dir()),
            key=lambda p: p.name,                # ISO timestamp prefix sorts chrono
        )

        # 1) Age-based purge
        cutoff = time.time() - self.max_age_days * 86400
        for d in list(all_dirs):
            try:
                mtime = d.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                all_dirs.remove(d)

        # 2) Count-based purge — keep newest max_sessions, delete the rest.
        # Two callers: begin_session (BEFORE the new manifest is written, so
        # we trim to make room for the incoming session) and explicit
        # `revio fix clean` (no incoming session — exact cap).
        # We're conservative: when cleanup is invoked at begin_session, the
        # current session_dir exists but is uncommitted (no manifest). We
        # subtract one from the cap if there's an in-flight uncommitted
        # session that's about to be committed, so the final committed count
        # lands at ≤ max_sessions.
        committed = [d for d in all_dirs if (d / "manifest.json").is_file()]
        uncommitted_count = sum(
            1 for d in all_dirs if not (d / "manifest.json").is_file()
        )
        target = max(1, self.max_sessions - uncommitted_count)
        if len(committed) > target:
            excess = len(committed) - target
            for d in committed[:excess]:    # oldest first
                shutil.rmtree(d, ignore_errors=True)

    # ---- Internals ----------------------------------------------------------

    def _session_dir(self) -> Path:
        assert self.session_id is not None
        return self.history_root / self.session_id

    def _snapshot_one(self, relpath: str, full_path: Path) -> FileSnapshot:
        """Copy one file's pre-apply content into snapshots/."""
        snap_dest = self._session_dir() / "snapshots" / relpath
        snap_dest.parent.mkdir(parents=True, exist_ok=True)

        if not full_path.is_file():
            # File doesn't exist yet — record "existed=False" so undo
            # knows to delete the file (which the fix will create).
            return FileSnapshot(relpath=relpath, existed=False)

        try:
            size = full_path.stat().st_size
        except OSError:
            size = 0
        if size > self.max_file_bytes:
            return FileSnapshot(relpath=relpath, existed=True, oversized=True)

        try:
            shutil.copy2(full_path, snap_dest)
        except OSError as e:
            logger.warning("fix_history: snapshot copy failed for %s: %s", relpath, e)
            return FileSnapshot(relpath=relpath, existed=True, oversized=False)

        return FileSnapshot(relpath=relpath, existed=True, oversized=False)

    def _read_session(self, session_dir: Path) -> FixSession | None:
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        files = [
            FileSnapshot(
                relpath=f["relpath"],
                existed=bool(f.get("existed", True)),
                oversized=bool(f.get("oversized", False)),
            )
            for f in data.get("files", [])
        ]
        return FixSession(
            session_id=data.get("session_id", session_dir.name),
            started_at=float(data.get("started_at", 0)),
            started_at_str=data.get("started_at_str", ""),
            repo_root=data.get("repo_root", ""),
            patchset_titles=list(data.get("patchset_titles", [])),
            files=files,
            _applied_ops_count=int(data.get("applied_ops_count", 0)),
        )
