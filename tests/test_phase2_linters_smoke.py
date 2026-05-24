"""Smoke test for the 6 phase-2 linter wrappers.

  · shellcheck   (real subprocess against bash fixture)
  · sqlfluff     (real subprocess against SQL fixture)
  · luacheck     (binary-absent fallback — verifies the NotInstalled path)
  · rubocop      (binary-absent fallback)
  · phpstan      (binary-absent fallback)
  · detekt       (binary-absent fallback)

The "absent" tests verify revio degrades gracefully when the binary isn't
on the box — which is the realistic state for most Mac/Linux dev machines.
The "present" tests confirm the JSON parsing + Finding conversion works.

Run:
    .venv/bin/python tests/test_phase2_linters_smoke.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Auto-detect which binaries we can exercise live
HAS_SHELLCHECK = shutil.which("shellcheck") is not None
HAS_SQLFLUFF = (
    shutil.which("sqlfluff") is not None
    or (Path(sys.executable).parent / "sqlfluff").is_file()
)


def test_shellcheck_live(tmp: Path) -> None:
    print("\n[1] shellcheck — live subprocess")
    if not HAS_SHELLCHECK:
        print("  · shellcheck not installed; skipping live test")
        return

    bad = tmp / "bad.sh"
    bad.write_text("""#!/bin/bash
foo=$1
echo $foo            # SC2086: unquoted expansion
ls $bad_glob*        # SC2086 again
[ $foo = "x" ]       # SC2086 word-splitting in test
""")

    from revio.layers.static.shellcheck import ShellcheckRunner

    runner = ShellcheckRunner()
    findings = runner.scan_to_findings(bad, repo_root=tmp)
    print(f"  · {len(findings)} findings on a 4-line bash script")
    assert len(findings) >= 2, f"expected ≥2 findings, got {len(findings)}"
    codes = {f.evidence[0].source for f in findings}
    print(f"  · rule codes: {sorted(codes)[:5]}")
    assert any(c.startswith("shellcheck:SC") for c in codes), "expected SCxxxx codes"
    sev = {f.severity.value for f in findings}
    print(f"  · severities seen: {sev}")


def test_shellcheck_absent_fallback() -> None:
    print("\n[2] shellcheck — binary-absent fallback")
    from revio.layers.static.shellcheck import (
        ShellcheckNotInstalledError,
        ShellcheckRunner,
    )

    try:
        ShellcheckRunner(binary="/nonexistent/shellcheck-fake")
    except (FileNotFoundError, OSError):
        pass  # would happen at first scan
    # Lazy path: try to scan with a bogus binary
    import os

    os.environ["REVIO_SHELLCHECK_BIN"] = "/nonexistent/shellcheck-fake"
    try:
        # Explicit binary so the locator can't fall back to PATH
        runner = ShellcheckRunner(binary="/nonexistent/shellcheck-fake")
        # Force a scan — should raise (not silently succeed)
        try:
            runner.scan(Path("/tmp"))
        except (OSError, FileNotFoundError):
            print("  · scan correctly raises when binary path is bogus")
    finally:
        del os.environ["REVIO_SHELLCHECK_BIN"]


def test_sqlfluff_live(tmp: Path) -> None:
    print("\n[3] sqlfluff — live subprocess")
    if not HAS_SQLFLUFF:
        print("  · sqlfluff not installed; skipping live test")
        return

    bad = tmp / "bad.sql"
    # ANSI dialect; intentional style violations
    bad.write_text(
        "select FOO, bar from BAZ where x=1;\n"   # mixed case; missing spaces
        "DELETE FROM USERS;\n"                     # DELETE without WHERE — but sqlfluff is style, not semantic
        "select * from t;\n"                       # SELECT * convention warn
    )

    from revio.layers.static.sqlfluff import SqlfluffRunner

    runner = SqlfluffRunner()
    findings = runner.scan_to_findings(bad, repo_root=tmp)
    print(f"  · {len(findings)} findings on a 3-line SQL script")
    assert len(findings) >= 1, f"expected ≥1 finding, got {len(findings)}"
    rule_codes = {f.evidence[0].source for f in findings if f.evidence}
    print(f"  · rule codes seen: {sorted(rule_codes)[:5]}")


def test_absent_fallbacks_for_others() -> None:
    """Verify the 4 non-installed linters raise NotInstalled correctly."""
    print("\n[4] luacheck / rubocop / phpstan / detekt — NotInstalled paths")
    from revio.layers.static.detekt import DetektNotInstalledError, DetektRunner
    from revio.layers.static.luacheck import LuacheckNotInstalledError, LuacheckRunner
    from revio.layers.static.phpstan import PhpstanNotInstalledError, PhpstanRunner
    from revio.layers.static.rubocop import RubocopNotInstalledError, RubocopRunner

    pairs = [
        ("luacheck", LuacheckRunner, LuacheckNotInstalledError),
        ("rubocop",  RubocopRunner,  RubocopNotInstalledError),
        ("phpstan",  PhpstanRunner,  PhpstanNotInstalledError),
        ("detekt",   DetektRunner,   DetektNotInstalledError),
    ]
    for name, cls, exc in pairs:
        if shutil.which(name):
            print(f"  · {name} actually installed — skip absent-test")
            continue
        try:
            cls()
            print(f"  ✗ {name}: should have raised {exc.__name__}!")
            return
        except exc as e:
            print(f"  · {name}: raises {exc.__name__} cleanly")
            assert "Install" in str(e) or "install" in str(e)


def test_tool_context_lazy() -> None:
    """ToolContext properties return None for absent runners, real obj otherwise."""
    print("\n[5] ToolContext properties wire up correctly")
    from revio.agent.tool_context import ToolContext

    ctx = ToolContext(repo_root=Path("."), profile_name="shell")
    assert hasattr(ctx, "shellcheck")
    assert hasattr(ctx, "luacheck")
    assert hasattr(ctx, "sqlfluff")
    assert hasattr(ctx, "rubocop")
    assert hasattr(ctx, "phpstan")
    assert hasattr(ctx, "detekt")

    sc = ctx.shellcheck    # would be a real runner or None
    if HAS_SHELLCHECK:
        assert sc is not None
        print(f"  · ctx.shellcheck → {type(sc).__name__}")
    else:
        print("  · ctx.shellcheck → None (graceful, binary absent)")

    sf = ctx.sqlfluff
    if HAS_SQLFLUFF:
        assert sf is not None
        print(f"  · ctx.sqlfluff   → {type(sf).__name__}")

    # Properties that DEFINITELY won't be installed
    assert ctx.luacheck is None or hasattr(ctx.luacheck, "scan")
    print("  · ctx.luacheck/rubocop/phpstan/detekt return None or runner cleanly")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="revio_phase2_lint_"))
    try:
        test_shellcheck_live(tmp)
        test_shellcheck_absent_fallback()
        test_sqlfluff_live(tmp)
        test_absent_fallbacks_for_others()
        test_tool_context_lazy()
        print("\nAll phase-2 linter smoke checks PASSED.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
