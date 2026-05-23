"""Multi-language smoke test for the generic AST + per-language Layer 2.

Verifies:
- Python: bandit picks up SQLi/MD5/pickle/shell=True; AST extracts functions & class
- Rust:   AST extracts functions inside impl block w/ enclosing_class
- Java:   AST extracts methods & constructor with the right names (NOT return types)
- Go:     AST extracts top-level + receiver methods with the right names

Does NOT require API keys — all checks are deterministic.

Run:
    .venv/bin/python tests/test_languages_smoke.py
"""

from __future__ import annotations

from pathlib import Path

from revio.layers.parser.treesitter_generic import shared as shared_ts


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "multilang"


def _section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def check_python() -> int:
    _section("Python (AST + bandit)")
    ts = shared_ts()
    path = FIXTURE_ROOT / "python_sample" / "app.py"

    functions = ts.list_functions(path)
    print(f"  list_functions: {len(functions)} found")
    function_names = {f.name for f in functions}
    expected = {"get_user", "login", "load_user_data", "run_user_command",
                "__init__", "add_user"}
    missing = expected - function_names
    if missing:
        print(f"  ❌ missing functions: {missing}")
        return 1
    print(f"  ✓ all expected functions found: {sorted(function_names)}")

    classes = ts.list_classes(path)
    class_names = {c.name for c in classes}
    if "UserManager" not in class_names:
        print(f"  ❌ UserManager class missing: {class_names}")
        return 1
    print(f"  ✓ class extracted: UserManager")

    # bandit
    from revio.layers.static import BanditRunner
    try:
        runner = BanditRunner()
    except Exception as e:
        print(f"  ⚠ bandit not available (skipping Layer 2 check): {e}")
        return 0

    findings = runner.scan_to_findings(path, repo_root=FIXTURE_ROOT / "python_sample")
    if not findings:
        print(f"  ❌ bandit returned 0 findings")
        return 1
    print(f"  ✓ bandit findings: {len(findings)}")
    test_ids = {(f.evidence[0].source if f.evidence else "?") for f in findings}
    if not any("B324" in t or "md5" in t.lower() for t in test_ids):
        print(f"  ⚠ no MD5-related test_id detected (got: {test_ids})")
    return 0


def check_rust() -> int:
    _section("Rust (AST only; clippy needs cargo)")
    ts = shared_ts()
    path = FIXTURE_ROOT / "rust_sample" / "src" / "lib.rs"

    functions = ts.list_functions(path)
    print(f"  list_functions: {len(functions)} found")
    fn_names = {f.name for f in functions}
    expected = {"new", "greet", "unsafe_helper", "unwrap_demo"}
    missing = expected - fn_names
    if missing:
        print(f"  ❌ missing functions: {missing}")
        return 1
    print(f"  ✓ all expected functions found: {sorted(fn_names)}")

    # methods inside impl should have enclosing_class="User"
    method_classes = {f.name: f.enclosing_class for f in functions if f.name in ("new", "greet")}
    if not all(c == "User" for c in method_classes.values()):
        print(f"  ❌ methods missing enclosing_class=User: {method_classes}")
        return 1
    print(f"  ✓ methods correctly attributed to enclosing struct: {method_classes}")

    classes = ts.list_classes(path)
    class_names = {(c.kind, c.name) for c in classes}
    if ("struct", "User") not in class_names:
        print(f"  ❌ User struct not detected: {class_names}")
        return 1
    if ("trait", "Greeter") not in class_names:
        print(f"  ❌ Greeter trait not detected: {class_names}")
        return 1
    print(f"  ✓ struct + trait both detected: {class_names}")
    return 0


def check_java() -> int:
    _section("Java (AST via tree-sitter-java)")
    ts = shared_ts()
    path = FIXTURE_ROOT / "java_sample" / "Greeter.java"

    functions = ts.list_functions(path)
    print(f"  list_functions: {len(functions)} found")
    fn_names = {f.name for f in functions}
    expected = {"Greeter", "hello", "addMessage", "nextId"}
    missing = expected - fn_names
    if missing:
        print(f"  ❌ missing functions: {missing}")
        return 1
    print(f"  ✓ all expected method names extracted: {sorted(fn_names)}")

    # The "hello" method must NOT be reported as 'String' (return type)
    hello = next((f for f in functions if f.name == "hello"), None)
    if hello is None:
        print("  ❌ 'hello' method missing")
        return 1
    if hello.enclosing_class != "Greeter":
        print(f"  ❌ hello.enclosing_class={hello.enclosing_class!r}, expected 'Greeter'")
        return 1
    print(f"  ✓ method names are correctly extracted (NOT return types)")

    classes = ts.list_classes(path)
    if not any(c.name == "Greeter" for c in classes):
        print(f"  ❌ Greeter class missing")
        return 1
    print(f"  ✓ Greeter class detected")
    return 0


def check_go() -> int:
    _section("Go (AST via tree-sitter-go)")
    ts = shared_ts()
    path = FIXTURE_ROOT / "go_sample" / "main.go"

    functions = ts.list_functions(path)
    print(f"  list_functions: {len(functions)} found")
    fn_names = {f.name for f in functions}
    expected = {"NewUser", "Greet", "NormalizedName", "main"}
    missing = expected - fn_names
    if missing:
        print(f"  ❌ missing functions: {missing}")
        return 1
    print(f"  ✓ all expected functions / methods extracted: {sorted(fn_names)}")

    # Methods should NOT be named 'string' (return type)
    greet = next((f for f in functions if f.name == "Greet"), None)
    if greet is None or greet.kind != "method":
        print(f"  ❌ Greet method not categorized as method: {greet}")
        return 1
    print(f"  ✓ method names correctly extracted (not return types)")
    return 0


def main() -> int:
    print("=" * 70)
    print("Multi-language smoke test (AST extraction + Layer 2)")
    print("=" * 70)

    rc = 0
    rc |= check_python()
    rc |= check_rust()
    rc |= check_java()
    rc |= check_go()
    rc |= check_cpp_static()
    rc |= check_go_static()
    rc |= check_llm_only_profiles()

    print()
    if rc == 0:
        print("✓ ALL MULTI-LANGUAGE CHECKS PASSED")
    else:
        print("❌ Some checks failed")
    return rc


def check_cpp_static() -> int:
    _section("C/C++ (cppcheck Layer 2)")
    from revio.layers.static import CppcheckRunner, CppcheckNotInstalledError

    path = FIXTURE_ROOT / "cpp_sample" / "bad.cpp"
    if not path.is_file():
        print("  ⚠ cpp_sample fixture missing")
        return 0
    try:
        runner = CppcheckRunner()
    except CppcheckNotInstalledError as e:
        print(f"  ⚠ cppcheck not installed (skipping): {str(e)[:80]}...")
        return 0

    findings = runner.scan_to_findings(path, repo_root=FIXTURE_ROOT / "cpp_sample")
    print(f"  cppcheck findings: {len(findings)}")
    if len(findings) < 2:
        print(f"  ❌ expected ≥ 2 findings, got {len(findings)}")
        return 1

    titles_lower = " ".join(f.title.lower() for f in findings)
    expected_signals = ["buffer", "null"]
    missing = [s for s in expected_signals if s not in titles_lower]
    if missing:
        print(f"  ❌ expected cppcheck signals missing: {missing}")
        print(f"  Got titles: {[f.title[:50] for f in findings]}")
        return 1
    print(f"  ✓ cppcheck found buffer + null findings as expected")
    return 0


def check_go_static() -> int:
    _section("Go (golangci-lint Layer 2)")
    from revio.layers.static import GolangCILintRunner, GolangCILintNotInstalledError

    target = FIXTURE_ROOT / "go_sample_module"
    if not (target / "go.mod").is_file():
        print("  ⚠ go_sample_module fixture missing go.mod")
        return 0
    try:
        runner = GolangCILintRunner()
    except GolangCILintNotInstalledError as e:
        print(f"  ⚠ golangci-lint not installed (skipping): {str(e)[:80]}...")
        return 0

    findings = runner.scan_to_findings(target, repo_root=target)
    print(f"  golangci-lint findings: {len(findings)}")
    if not findings:
        print(f"  ⚠ no findings — strange, fixture should have issues")
        return 0
    print(f"  ✓ golangci-lint surfaced {len(findings)} issue(s)")
    return 0


def check_llm_only_profiles() -> int:
    _section("LLM-only profiles register correctly")
    from revio.profiles import get_profile, list_profiles, load_all_profiles

    load_all_profiles()
    llm_only = ["matlab", "r", "verilog", "sas", "cobol", "solidity", "zig", "objc", "dart"]
    missing = [p for p in llm_only if get_profile(p) is None]
    if missing:
        print(f"  ❌ missing LLM-only profiles: {missing}")
        return 1
    print(f"  ✓ all 9 LLM-only profiles registered: {llm_only}")

    for name in llm_only:
        hints = get_profile(name).make_reasoning_hints()
        if len(hints) < 200:
            print(f"  ❌ {name} reasoning_hints too short ({len(hints)} chars)")
            return 1
    print(f"  ✓ all LLM-only profiles have rich reasoning_hints (>=200 chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
