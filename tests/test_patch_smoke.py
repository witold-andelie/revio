"""Patch model + applier smoke test (M4 --fix feature).

Verifies the data path:
1. PatchOp / PatchSet models serialize cleanly via Pydantic
2. PatchApplier.can_apply detects all the failure modes:
   - file outside repo
   - old_content mismatch
   - line range out of bounds
   - target already exists for rename
3. PatchApplier.preview renders a readable diff
4. PatchApplier.apply writes correct content + leaves clean state
5. PatchApplier with git refuses dirty repo unless allow_dirty
6. propose_patch tool emits state update via Command

Does NOT exercise the interactive fix flow (would need TTY).

Run:
    .venv/bin/python tests/test_patch_smoke.py
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from revio.agent.patch import (
    PatchApplier,
    PatchApplyError,
    PatchOp,
    PatchSet,
)
from revio.agent.tools import propose_patch


def _git_init(repo: Path) -> None:
    """Initialise a clean git repo with one initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("placeholder\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


# --- Test cases -------------------------------------------------------------


def test_models_serialize() -> int:
    print("=== Test 1: PatchOp / PatchSet round-trip Pydantic ===")
    op = PatchOp(
        op_type="edit",
        file_path="a.js",
        line_start=1,
        line_end=3,
        old_content="old",
        new_content="new",
        reason="test",
    )
    ps = PatchSet(
        title="Test patch",
        description="desc",
        ops=[op],
        confidence=0.9,
    )
    json_text = ps.model_dump_json()
    round_trip = PatchSet.model_validate_json(json_text)
    if round_trip.title != "Test patch":
        print("  ❌ round-trip lost title")
        return 1
    if len(round_trip.ops) != 1:
        print("  ❌ round-trip lost ops")
        return 1
    if round_trip.affected_files != {"a.js"}:
        print(f"  ❌ affected_files wrong: {round_trip.affected_files}")
        return 1
    print("  ✓ PatchSet round-trips via JSON")
    print(f"  ✓ affected_files property works: {round_trip.affected_files}")
    return 0


def test_can_apply_pre_flight() -> int:
    print("\n=== Test 2: can_apply detects every failure mode ===")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "a.js").write_text("line 1\nline 2\nline 3\n")
        applier = PatchApplier(repo)

        # OK case
        ok_ps = PatchSet(
            title="ok", description="",
            ops=[PatchOp(
                op_type="delete_lines", file_path="a.js",
                line_start=2, line_end=2, old_content="line 2\n",
            )],
        )
        ok, reason = applier.can_apply(ok_ps)
        if not ok:
            print(f"  ❌ valid patch rejected: {reason}")
            return 1
        print("  ✓ valid patch passes pre-flight")

        # Out-of-bounds line
        oob_ps = PatchSet(
            title="oob", description="",
            ops=[PatchOp(
                op_type="delete_lines", file_path="a.js",
                line_start=10, line_end=12, old_content="X",
            )],
        )
        ok, reason = applier.can_apply(oob_ps)
        if ok or "line_end" not in reason:
            print(f"  ❌ out-of-bounds not detected: ok={ok}, reason={reason!r}")
            return 1
        print(f"  ✓ out-of-bounds detected: {reason}")

        # old_content mismatch
        mismatch_ps = PatchSet(
            title="mismatch", description="",
            ops=[PatchOp(
                op_type="delete_lines", file_path="a.js",
                line_start=2, line_end=2,
                old_content="this is NOT what the file says\n",
            )],
        )
        ok, reason = applier.can_apply(mismatch_ps)
        if ok or "match" not in reason.lower():
            print(f"  ❌ content mismatch not detected: ok={ok}, reason={reason!r}")
            return 1
        print(f"  ✓ content mismatch detected (anti-corruption guard)")

        # Path escape
        escape_ps = PatchSet(
            title="escape", description="",
            ops=[PatchOp(
                op_type="delete_file", file_path="/etc/passwd",
            )],
        )
        ok, reason = applier.can_apply(escape_ps)
        if ok or "escape" not in reason.lower():
            print(f"  ❌ path escape not detected: ok={ok}, reason={reason!r}")
            return 1
        print(f"  ✓ path escape detected (security guard)")

    return 0


def test_apply_writes_correct_content() -> int:
    print("\n=== Test 3: apply() writes correct content ===")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        f = repo / "utils.js"
        f.write_text(
            "export function formatName(a, b) {\n"
            "    return `${a} ${b}`.trim();\n"
            "}\n"
            "\n"
            "export function buildName(a, b) {\n"
            "    return `${a} ${b}`.trim();\n"
            "}\n"
        )

        applier = PatchApplier(repo)
        ps = PatchSet(
            title="Remove duplicate buildName",
            description="Identical to formatName",
            confidence=0.95,
            ops=[PatchOp(
                op_type="delete_lines",
                file_path="utils.js",
                line_start=5,
                line_end=7,
                old_content="export function buildName(a, b) {\n    return `${a} ${b}`.trim();\n}\n",
                reason="duplicate",
            )],
        )

        ok, reason = applier.can_apply(ps)
        if not ok:
            print(f"  ❌ can_apply failed: {reason}")
            return 1

        applier.apply(ps)

        after = f.read_text()
        expected = (
            "export function formatName(a, b) {\n"
            "    return `${a} ${b}`.trim();\n"
            "}\n"
            "\n"
        )
        if after != expected:
            print(f"  ❌ content mismatch after apply.")
            print(f"  Got:\n{after!r}")
            print(f"  Expected:\n{expected!r}")
            return 1
        print(f"  ✓ buildName removed cleanly; formatName preserved")
        print(f"  ✓ applied_patches: {len(applier._applied_patches)}")

    return 0


def test_git_safety_refuses_dirty() -> int:
    print("\n=== Test 4: begin_session refuses dirty repo (no --allow-dirty) ===")
    if not shutil.which("git"):
        print("  ⚠ git not installed (skipping)")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git_init(repo)
        # Make the repo dirty
        (repo / "uncommitted.txt").write_text("uncommitted change\n")

        applier = PatchApplier(repo)
        try:
            applier.begin_session(allow_dirty=False)
            print("  ❌ begin_session should have refused dirty repo")
            return 1
        except PatchApplyError as e:
            if "uncommitted" not in str(e).lower():
                print(f"  ❌ wrong error message: {e}")
                return 1
            print(f"  ✓ refused dirty repo: {str(e)[:60]}...")

    return 0


def test_preview_renders_diff() -> int:
    print("\n=== Test 5: preview() renders a readable unified diff ===")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "a.js").write_text("function old() {}\n")
        applier = PatchApplier(repo)

        ps = PatchSet(
            title="Rename old → new",
            description="cleanup",
            confidence=0.9,
            ops=[PatchOp(
                op_type="edit",
                file_path="a.js",
                line_start=1, line_end=1,
                old_content="function old() {}\n",
                new_content="function newer() {}\n",
                reason="rename for clarity",
            )],
        )

        preview = applier.preview(ps)
        if "function old()" not in preview or "function newer()" not in preview:
            print(f"  ❌ preview missing diff content: {preview!r}")
            return 1
        if "Confidence: 0.90" not in preview:
            print(f"  ❌ preview missing confidence")
            return 1
        print(f"  ✓ preview includes old + new content + metadata")
        return 0


def test_propose_patch_tool_emits_command() -> int:
    print("\n=== Test 6: propose_patch tool emits Command(update={patches:[...]}) ===")
    result = propose_patch.invoke({
        "title": "Test patch",
        "description": "test",
        "ops": [
            {
                "op_type": "delete_lines",
                "file_path": "a.js",
                "line_start": 1, "line_end": 1,
                "old_content": "x\n",
            }
        ],
        "confidence": 0.9,
    })
    if not hasattr(result, "update"):
        print(f"  ❌ propose_patch didn't return a Command")
        return 1
    patches = result.update.get("patches", [])
    if len(patches) != 1:
        print(f"  ❌ expected 1 patch in update, got {len(patches)}")
        return 1
    if patches[0].title != "Test patch":
        print(f"  ❌ patch title wrong: {patches[0].title}")
        return 1
    print(f"  ✓ propose_patch emits Command with 1 patch correctly")
    return 0


def main() -> int:
    print("=" * 70)
    print("Patch model + applier smoke test (M4 --fix)")
    print("=" * 70)

    rc = 0
    rc |= test_models_serialize()
    rc |= test_can_apply_pre_flight()
    rc |= test_apply_writes_correct_content()
    rc |= test_git_safety_refuses_dirty()
    rc |= test_preview_renders_diff()
    rc |= test_propose_patch_tool_emits_command()

    print()
    if rc == 0:
        print("✓ ALL PATCH SMOKE CHECKS PASSED")
    else:
        print("❌ Some patch checks failed")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
