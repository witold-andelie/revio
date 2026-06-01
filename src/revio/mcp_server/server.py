"""FastMCP-based MCP server exposing revio's core capabilities.

# What this server is for

revio's main UX is a CLI. Sometimes you want revio's review power
embedded in someone else's agent loop — Claude Code, Cursor, a custom
LangGraph workflow, an IDE plugin. The MCP protocol is the lingua franca
for that, so we expose revio's pipelines as MCP tools.

# Tools exposed

  · revio_audit            — full-repo security audit (LLM + Layer 2)
  · revio_review           — diff-scoped review
  · revio_dedup            — AI-redundancy scan (returns findings + patches,
                              does NOT apply them — caller decides)
  · revio_run_<analyzer>   — one tool per Layer 2 analyzer (13 total):
                              bandit, oxlint, cppcheck, clippy, spotbugs,
                              golangci_lint, shellcheck, luacheck, sqlfluff,
                              rubocop, phpstan, detekt, verilator
  · revio_search_guidelines — RAG query over indexed org guidelines
  · revio_list_profiles    — what languages/profiles are available
  · revio_detect_profile   — auto-detect the right profile for a repo

# Design choices

1. The full LLM-driven pipelines (audit/review/dedup) take 30-60s — they
   are exposed as MCP tools but the host agent should treat them as
   "long-running operations". MCP supports this fine.

2. The "run individual analyzer" tools (13 of them, one per Layer 2
   backend) are SYNCHRONOUS and FAST (~1-3s). They DO NOT call the LLM
   and are free for the host agent to call casually. New analyzers are
   added by registering them in `_ANALYZER_RUNNERS` below; the @mcp.tool
   wrapper is generated automatically.

3. We deliberately do NOT expose `revio_dedup --fix` (the apply step).
   The host agent should call `revio_dedup`, inspect the returned
   patches, and decide whether to apply via its own filesystem tools.
   This keeps the security model clean: revio's MCP server never
   mutates files; it only reports.

4. stdout is reserved for JSON-RPC. All logging goes to stderr. Do not
   print() anywhere in this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def _load_config():
    """Lazy-load config — must run AFTER server boots so cwd is correct."""
    from ..config import load_config

    return load_config()


# Registry of every Layer-2 analyzer exposed as its own MCP tool.
# Tuple: (module suffix under revio.layers.static, RunnerClass attr name,
#         entry method name, language blurb for the tool description).
_ANALYZER_RUNNERS: dict[str, tuple[str, str, str, str]] = {
    "bandit":        ("bandit",        "BanditRunner",       "scan_to_findings", "Python security scan"),
    "oxlint":        ("oxlint",        "OxlintRunner",       "lint_to_findings", "JS/TS lint"),
    "cppcheck":      ("cppcheck",      "CppcheckRunner",     "scan_to_findings", "C/C++ analysis"),
    "clippy":        ("clippy",        "ClippyRunner",       "scan_to_findings", "Rust lint via cargo clippy"),
    "spotbugs":      ("spotbugs",      "SpotBugsRunner",     "scan_to_findings", "Java bytecode analysis (needs .class)"),
    "golangci_lint": ("golangci_lint", "GolangCILintRunner", "scan_to_findings", "Go meta-linter"),
    "shellcheck":    ("shellcheck",    "ShellcheckRunner",   "scan_to_findings", "Shell script analysis"),
    "luacheck":      ("luacheck",      "LuacheckRunner",     "scan_to_findings", "Lua lint"),
    "sqlfluff":      ("sqlfluff",      "SqlfluffRunner",     "scan_to_findings", "SQL lint (multi-dialect)"),
    "rubocop":       ("rubocop",       "RubocopRunner",      "scan_to_findings", "Ruby lint"),
    "phpstan":       ("phpstan",       "PhpstanRunner",      "scan_to_findings", "PHP static analysis"),
    "detekt":        ("detekt",        "DetektRunner",       "scan_to_findings", "Kotlin lint (needs JDK)"),
    "verilator":     ("verilator",     "VerilatorRunner",    "scan_to_findings", "Verilog/SystemVerilog --lint-only"),
}


def build_server() -> FastMCP:
    """Construct the FastMCP server with all revio tools registered."""
    mcp = FastMCP(
        name="revio",
        instructions=(
            "revio exposes agentic code-review capabilities. Call "
            "revio_audit for a full security scan, revio_dedup to find "
            "AI-generated redundancy, or revio_review to assess a diff. "
            "For cheap Layer-2-only analysis without the LLM cost, call "
            "revio_run_<analyzer> (13 tools, one per backend: bandit, "
            "oxlint, cppcheck, clippy, spotbugs, golangci_lint, shellcheck, "
            "luacheck, sqlfluff, rubocop, phpstan, detekt, verilator). "
            "All paths must be absolute."
        ),
    )

    # --- Full-pipeline tools (slow, LLM-backed) ------------------------------

    @mcp.tool(
        name="revio_audit",
        description=(
            "Run a full-repo security audit using revio's agent loop. "
            "Returns findings as JSON. Takes 30-60s per repo. "
            "repo_path must be an absolute filesystem path. "
            "profile is one of 'auto', 'python', 'js', 'cpp', 'plc', "
            "'java', 'go', 'rust' (default: auto-detect)."
        ),
    )
    async def revio_audit(
        repo_path: str,
        profile: str = "auto",
        budget: int = 12,
    ) -> str:
        return await _run_pipeline("audit", repo_path, profile=profile, budget=budget)

    @mcp.tool(
        name="revio_review",
        description=(
            "Review a diff or commit. Targets HEAD~1..HEAD by default. "
            "Returns findings as JSON. Takes 20-45s. "
            "repo_path must be absolute; base_ref is a git ref like 'HEAD~1' "
            "or a branch name."
        ),
    )
    async def revio_review(
        repo_path: str,
        base_ref: str = "HEAD~1",
        profile: str = "auto",
        budget: int = 10,
    ) -> str:
        return await _run_pipeline(
            "review", repo_path, target_ref=base_ref, profile=profile, budget=budget
        )

    @mcp.tool(
        name="revio_dedup",
        description=(
            "Scan for AI-generated redundancy (duplicate functions, dead "
            "code, useless wrappers). Returns findings AND structured patch "
            "operations (delete_lines / edit / etc) as JSON. Does NOT apply "
            "the patches — your agent should inspect them and decide. "
            "Best with profile='js' or 'python'."
        ),
    )
    async def revio_dedup(
        repo_path: str,
        profile: str = "js",
        budget: int = 15,
    ) -> str:
        return await _run_pipeline(
            "dedup", repo_path, profile=profile, budget=budget, include_patches=True
        )

    # --- Layer-2-only tools (fast, no LLM) -----------------------------------
    # One @mcp.tool per analyzer in _ANALYZER_RUNNERS, generated in a loop.
    # Factory function closes over `analyzer_name` per iteration; FastMCP
    # disallows leading-underscore parameters, so a fresh inner def per
    # analyzer is the cleanest binding strategy.

    def _make_analyzer_tool(analyzer_name: str):
        def runner(path: str) -> str:
            return _run_static_analyzer(analyzer_name, path)
        return runner

    for name, (_mod, _cls, _method, blurb) in _ANALYZER_RUNNERS.items():
        mcp.tool(
            name=f"revio_run_{name}",
            description=(
                f"Run {name} — {blurb}. Fast (~1-3s), no LLM cost. "
                f"path must be absolute, can be a file or directory. "
                f"Returns findings as JSON."
            ),
        )(_make_analyzer_tool(name))

    # --- Discovery / context tools (instant) ---------------------------------

    @mcp.tool(
        name="revio_list_profiles",
        description=(
            "List all available revio profiles (languages and PLC vendors). "
            "Returns a JSON array of {name, description, primary_extensions}."
        ),
    )
    def revio_list_profiles() -> str:
        from ..profiles import load_all_profiles, list_profiles

        load_all_profiles()
        out = []
        for name in sorted(list_profiles()):
            try:
                from ..profiles import get_profile

                cls = get_profile(name)
                doc = (cls.__doc__ or "").strip().split("\n")[0] if cls else ""
                exts = list(getattr(cls, "extensions", []) or []) if cls else []
                out.append({"name": name, "description": doc, "extensions": exts})
            except Exception as e:
                out.append({"name": name, "description": "", "error": str(e)})
        return json.dumps(out, indent=2)

    @mcp.tool(
        name="revio_detect_profile",
        description=(
            "Auto-detect the best revio profile for a repository based on "
            "its file mix. Returns JSON with the suggested profile name and "
            "supporting fingerprint info. repo_path must be absolute."
        ),
    )
    def revio_detect_profile(repo_path: str) -> str:
        from ..detect import detect_project

        fp = detect_project(repo_path)
        return json.dumps(fp.model_dump(), indent=2)

    @mcp.tool(
        name="revio_search_guidelines",
        description=(
            "Query the RAG-indexed org coding guidelines for this repo. "
            "Returns top-k passages as JSON. Use this BEFORE proposing big "
            "changes, so suggestions align with the org's documented "
            "standards. Returns an empty list if no guidelines are indexed."
        ),
    )
    def revio_search_guidelines(repo_path: str, query: str, k: int = 5) -> str:
        try:
            from ..layers.parser.rag.guidelines import GuidelinesStore

            store = GuidelinesStore(Path(repo_path))
            hits = store.search(query, k=k)
            return json.dumps(
                [
                    {"source": h.source, "text": h.text, "score": h.score}
                    for h in hits
                ],
                indent=2,
            )
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}", "hits": []})

    return mcp


# --- Internal helpers --------------------------------------------------------


async def _run_pipeline(
    mode: str,
    repo_path: str,
    *,
    target_ref: str = "",
    profile: str = "auto",
    budget: int = 12,
    include_patches: bool = False,
) -> str:
    """Common runner for audit/review/dedup tool handlers."""
    abs_repo = Path(repo_path).expanduser().resolve()
    if not abs_repo.exists():
        return json.dumps({"error": f"repo_path does not exist: {abs_repo}"})

    cfg = _load_config()
    # Cap budget per request so a hostile client can't burn through credits
    cfg.agent.max_tool_calls = max(1, min(budget, 30))

    from ..agent.runner import run_agent

    try:
        report = await run_agent(
            mode=mode,
            repo_path=str(abs_repo),
            target_ref=target_ref,
            profile_name=None if profile == "auto" else profile,
            config=cfg,
            on_event=lambda _e, _p: None,
        )
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}", "mode": mode})

    payload = report.model_dump(mode="json")
    if not include_patches:
        payload.pop("patches", None)
    return json.dumps(payload, indent=2, default=str)


def _run_static_analyzer(name: str, path: str) -> str:
    """Dispatch one analyzer (registered in _ANALYZER_RUNNERS) and return
    its findings as JSON."""
    abs_path = Path(path).expanduser().resolve()
    if not abs_path.exists():
        return json.dumps({"error": f"path does not exist: {abs_path}"})

    spec = _ANALYZER_RUNNERS.get(name)
    if spec is None:
        return json.dumps({"error": f"unknown analyzer: {name}"})
    mod_suffix, cls_name, method_name, _blurb = spec

    repo_root = abs_path if abs_path.is_dir() else abs_path.parent

    try:
        import importlib

        mod = importlib.import_module(f"revio.layers.static.{mod_suffix}")
        runner = getattr(mod, cls_name)()
        findings = getattr(runner, method_name)(abs_path, repo_root=repo_root)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}", "analyzer": name})

    return json.dumps(
        {
            "analyzer": name,
            "path": str(abs_path),
            "count": len(findings),
            "findings": [f.model_dump(mode="json") for f in findings],
        },
        indent=2,
        default=str,
    )


# --- Entry point -------------------------------------------------------------


def run_stdio() -> None:
    """Synchronous entry point — block on stdio MCP server loop.

    Called by `revio mcp-server` CLI subcommand. stdout is the JSON-RPC
    channel; all logging must go to stderr (configured via the root
    logger in revio's CLI bootstrap).
    """
    import logging

    # Ensure no log output sneaks into stdout
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    server = build_server()
    server.run("stdio")
