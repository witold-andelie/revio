"""Smoke test for fix history: snapshot, undo, undo-of-undo, auto-rotation.

Covers:
  · `revio dedup --fix` records a session and you can undo it
  · Works WITHOUT a git repo (the whole point of this feature)
  · Multiple sessions stack chronologically
  · Undo restores file contents byte-for-byte
  · Undo itself is a session, so it can be re-done
  · Auto-rotation respects max_sessions
  · Files >max_file_bytes are flagged 'oversized' and skipped on undo

Run:
    .venv/bin/python tests/test_fix_history_smoke.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from revio.agent.fix_history import FixHistoryStore
from revio.agent.patch import PatchApplier, PatchOp, PatchSet


def _make_patchset(file_path: str, line_start: int, line_end: int,
                   old: str, new: str, title: str = "test patch") -> PatchSet:
    return PatchSet(
        title=title,
        description="test",
        confidence=0.95,
        ops=[
            PatchOp(
                op_type="edit",
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                old_content=old,
                new_content=new,
                reason="test reason",
            )
        ],
    )


def test_basic_undo_no_git(repo: Path, cache: Path):
    print("\n[1] basic apply → undo, no git involved")
    (repo / "a.py").write_text("line 1\nline 2\nline 3\n")

    store = FixHistoryStore(repo_root=repo, checkpoint_dir=cache,
                            max_sessions=50, max_age_days=30)
    applier = PatchApplier(repo, history_store=store)
    applier.begin_session()
    ps = _make_patchset("a.py", 2, 2, "line 2\n", "LINE TWO!\n", "rename L2")
    applier.apply(ps)
    applier.end_session()

    assert (repo / "a.py").read_text() == "line 1\nLINE TWO!\nline 3\n", "patch did not apply"
    print("  · patch applied, file content is the modified version")

    sessions = store.list_sessions()
    assert len(sessions) == 1, f"expected 1 session, got {len(sessions)}"
    sess = sessions[0]
    assert sess.file_count == 1
    assert sess.applied_ops_count == 1
    print(f"  · session recorded: {sess.session_id}")

    # Undo
    restored, warnings = store.undo_session(sess.session_id)
    assert restored == 1, f"expected 1 file restored, got {restored}"
    assert not warnings, f"unexpected warnings: {warnings}"
    assert (repo / "a.py").read_text() == "line 1\nline 2\nline 3\n", "undo did not restore"
    print("  · undo restored byte-for-byte")


def test_undo_of_undo(repo: Path, cache: Path):
    print("\n[2] undo of undo = redo")
    (repo / "b.py").write_text("hello\n")
    store = FixHistoryStore(repo_root=repo, checkpoint_dir=cache,
                            max_sessions=50, max_age_days=30)

    applier = PatchApplier(repo, history_store=store)
    applier.begin_session()
    applier.apply(_make_patchset("b.py", 1, 1, "hello\n", "HELLO!\n", "shout"))
    applier.end_session()
    assert (repo / "b.py").read_text() == "HELLO!\n"

    sessions_after_fix = store.list_sessions()
    fix_id = sessions_after_fix[0].session_id
    print(f"  · fix session: {fix_id}")

    # Undo the fix
    store.undo_session(fix_id)
    assert (repo / "b.py").read_text() == "hello\n"
    print("  · undo restored to 'hello'")

    # Undo the undo — should redo the fix
    sessions_after_undo = store.list_sessions()
    undo_id = sessions_after_undo[0].session_id  # newest
    assert undo_id != fix_id
    store.undo_session(undo_id)
    assert (repo / "b.py").read_text() == "HELLO!\n", \
        "undo-of-undo should re-apply original fix"
    print("  · undo-of-undo re-applied 'HELLO!'")


def test_multiple_sessions_independent(repo: Path, cache: Path):
    print("\n[3] multiple sessions stack chronologically, can target old one")
    (repo / "c.py").write_text("v0\n")
    store = FixHistoryStore(repo_root=repo, checkpoint_dir=cache,
                            max_sessions=50, max_age_days=30)

    for i in range(1, 4):
        applier = PatchApplier(repo, history_store=store)
        applier.begin_session()
        old = f"v{i-1}\n" if i == 1 else f"v{i-1}\n"
        applier.apply(_make_patchset("c.py", 1, 1, old, f"v{i}\n", f"bump to v{i}"))
        applier.end_session()
        time.sleep(0.05)  # ensure distinct session IDs

    assert (repo / "c.py").read_text() == "v3\n"
    sessions = store.list_sessions()
    fix_sessions = [s for s in sessions if "bump" in (s.patchset_titles[0] if s.patchset_titles else "")]
    assert len(fix_sessions) == 3, f"expected 3 bump sessions, got {len(fix_sessions)}"
    print(f"  · 3 sequential bumps recorded; current = v3")

    # Undo the first one — should restore v0 (snapshot was taken before v0→v1)
    oldest_bump_id = fix_sessions[-1].session_id  # list_sessions sorts newest first
    print(f"  · undoing oldest bump session: {oldest_bump_id}")
    store.undo_session(oldest_bump_id)
    assert (repo / "c.py").read_text() == "v0\n", \
        f"undo of oldest should restore v0, got {(repo/'c.py').read_text()!r}"
    print("  · file restored to v0 by selecting old session, not just the most recent")


def test_oversized_file_skipped(repo: Path, cache: Path):
    print("\n[4] oversized file is flagged + skipped on undo")
    big = repo / "big.txt"
    big.write_text("x" * 500_000)   # 500 KB

    store = FixHistoryStore(repo_root=repo, checkpoint_dir=cache,
                            max_sessions=50, max_age_days=30,
                            max_file_bytes=100_000)  # cap below file size

    applier = PatchApplier(repo, history_store=store)
    applier.begin_session()
    ps = _make_patchset("big.txt", 1, 1, "x" * 500_000, "y" * 500_000, "rewrite")
    applier.apply(ps)
    applier.end_session()

    sess = store.list_sessions()[0]
    big_entry = next(f for f in sess.files if f.relpath == "big.txt")
    assert big_entry.oversized, "big file should be flagged oversized"
    print("  · big file flagged oversized in manifest")

    restored, warnings = store.undo_session(sess.session_id)
    assert any("big.txt" in w for w in warnings), f"expected oversized warning, got {warnings}"
    assert restored == 0, "no file should have been restored"
    print(f"  · undo skipped with warning: {warnings[0]}")


def test_auto_rotation(repo: Path, cache: Path):
    print("\n[5] auto-rotation respects max_sessions")
    f = repo / "rot.py"
    f.write_text("0\n")

    store = FixHistoryStore(repo_root=repo, checkpoint_dir=cache,
                            max_sessions=3, max_age_days=30)

    # Create 5 sessions; only 3 newest should survive
    for i in range(1, 6):
        applier = PatchApplier(repo, history_store=store)
        applier.begin_session()
        applier.apply(_make_patchset("rot.py", 1, 1, f"{i-1}\n", f"{i}\n",
                                     f"bump to {i}"))
        applier.end_session()
        time.sleep(0.05)

    sessions = store.list_sessions(limit=None)
    # The undo of nothing being added to count too, but in this test none were undone
    bump_sessions = [s for s in sessions if s.patchset_titles and s.patchset_titles[0].startswith("bump")]
    assert len(bump_sessions) <= 3, \
        f"expected ≤3 sessions after rotation, got {len(bump_sessions)}"
    print(f"  · 5 created, {len(bump_sessions)} retained (cap=3) ✓")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="revio_fix_history_test_"))
    print(f"work dir: {tmp}")
    try:
        test_basic_undo_no_git(tmp / "r1", _setup(tmp / "r1"))
        test_undo_of_undo(tmp / "r2", _setup(tmp / "r2"))
        test_multiple_sessions_independent(tmp / "r3", _setup(tmp / "r3"))
        test_oversized_file_skipped(tmp / "r4", _setup(tmp / "r4"))
        test_auto_rotation(tmp / "r5", _setup(tmp / "r5"))
        print("\nAll fix-history smoke checks PASSED.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _setup(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    cache = repo.parent / f"{repo.name}_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


if __name__ == "__main__":
    sys.exit(main())
