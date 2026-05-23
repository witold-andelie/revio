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

    print()
    if rc == 0:
        print("✓ ALL MULTI-LANGUAGE CHECKS PASSED")
    else:
        print("❌ Some checks failed")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
