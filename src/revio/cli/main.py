"""Typer CLI entry point.

Subcommands:
    revio                       → interactive REPL (no args)
    revio review [path] [opts]  → one-shot diff review
    revio audit  [path] [opts]  → one-shot full-repo audit
    revio dedup  [path] [opts]  → one-shot AI-redundancy scan
    revio config init           → re-run the setup wizard
    revio config show           → print current config
    revio config edit           → open config in $EDITOR
    revio config path           → print config file path

All user-facing output is English.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.console import Console

from ..config import (
    Config,
    config_exists,
    load_config,
    user_config_path,
)


_console = Console()
_err_console = Console(stderr=True, style="red")


# --- Typer apps ---------------------------------------------------------------


app = typer.Typer(
    name="revio",
    help="Agentic code-review CLI (LangGraph-powered).",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)

config_app = typer.Typer(help="Manage revio's configuration.")
app.add_typer(config_app, name="config")

guidelines_app = typer.Typer(help="Manage the RAG index over your coding guidelines.")
app.add_typer(guidelines_app, name="guidelines")

skills_app = typer.Typer(help="Manage agent skills (Anthropic Agent Skills spec).")
app.add_typer(skills_app, name="skills")


# --- Root callback ------------------------------------------------------------


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context):
    """No subcommand → drop into the REPL."""
    if ctx.invoked_subcommand is not None:
        return

    # First-run? Trigger wizard before REPL.
    if not config_exists():
        from .wizard import run_wizard

        cfg = run_wizard()
        if cfg is None:
            raise typer.Exit(code=1)

    # Lazy import REPL to avoid prompt_toolkit overhead for one-shot commands
    from .repl import run_repl

    run_repl()


# --- review / audit / dedup ---------------------------------------------------


def _ensure_config() -> Config:
    """Load config, running wizard if missing."""
    if not config_exists():
        from .wizard import run_wizard

        cfg = run_wizard()
        if cfg is None:
            raise typer.Exit(code=1)
        return cfg
    return load_config(Path.cwd())


def _handle_non_git(repo_path: Path) -> str | None:
    """If repo_path isn't a git repo, ask the user what to do.

    Returns:
        "init" → user wants us to git-init the dir
        "scan" → user wants file-scan mode (no diff)
        None   → user cancelled
    """
    import git

    try:
        git.Repo(repo_path)
        return "git"  # already a valid repo
    except Exception:
        pass

    _console.print()
    _console.print(f"  [yellow]⚠[/]  [bold]{repo_path}[/] is not a git repository.")
    _console.print("  The agent normally works on diffs, so it needs git history.")
    _console.print()

    choice = questionary.select(
        "What would you like to do?",
        choices=[
            "Initialize a git repo here (git init + initial commit)",
            "Just review the current files (file-scan mode, no diff)",
            "Cancel — let me set up git myself first",
        ],
    ).ask()
    if choice is None:
        return None
    if "Initialize" in choice:
        return "init"
    if "file-scan" in choice:
        return "scan"
    return None


def _git_init_with_commit(repo_path: Path) -> bool:
    """Run git init + initial commit."""
    import git

    try:
        repo = git.Repo.init(repo_path)
        gitignore = repo_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "node_modules/\n__pycache__/\n.git/\nvenv/\n.venv/\n"
                "dist/\nbuild/\n*.pyc\n*.pyo\n.idea/\n.vscode/\n",
                encoding="utf-8",
            )
        repo.git.add(A=True)
        repo.index.commit("Initial commit (revio auto-init)")
        _console.print(f"  [green]✓[/] Initialized git repo at {repo_path}")
        return True
    except Exception as e:
        _err_console.print(f"  ✗ git init failed: {e}")
        return False


def _run(
    mode: str,
    path: Path,
    target_ref: str,
    output_format: str,
    output_path: Optional[Path],
    profile: Optional[str],
    budget: Optional[int],
):
    """Shared body for the three mode subcommands."""
    cfg = _ensure_config()

    repo_path = path.expanduser().resolve()
    if not repo_path.is_dir():
        _err_console.print(f"  ✗ Path does not exist: {repo_path}")
        raise typer.Exit(code=1)

    # Git handling for review mode (audit/dedup work on any dir)
    if mode == "review":
        decision = _handle_non_git(repo_path)
        if decision is None:
            raise typer.Exit(code=1)
        if decision == "init":
            if not _git_init_with_commit(repo_path):
                raise typer.Exit(code=1)
        elif decision == "scan":
            # Downgrade to file-scan style: no commit ref
            target_ref = ""

    # Build per-call overrides
    if budget is not None:
        cfg = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_tool_calls": budget})})

    profile_to_use: str = profile or cfg.profile.default

    # Build renderer based on format
    from ..output.stream import StreamRenderer, format_as_json, format_as_markdown
    from ..agent import run_agent_sync

    renderer = StreamRenderer(_console) if output_format == "stream" else StreamRenderer(_console, verbose=False)

    on_event = renderer.handle if output_format == "stream" else (lambda e, p: None)

    try:
        report = run_agent_sync(
            mode=mode,
            repo_path=str(repo_path),
            target_ref=target_ref,
            profile_name=profile_to_use,
            config=cfg,
            on_event=on_event,
        )
    except KeyboardInterrupt:
        _console.print("\n[yellow]Interrupted by user.[/]")
        raise typer.Exit(code=130)
    except Exception as e:
        _err_console.print(f"\n  ✗ Agent failed: {e}")
        if os.environ.get("REVIO_DEBUG"):
            import traceback

            traceback.print_exc()
        raise typer.Exit(code=1)

    # Format outputs other than stream
    if output_format == "json":
        text = format_as_json(report)
    elif output_format == "markdown":
        text = format_as_markdown(report)
    else:
        text = ""  # stream already wrote everything

    if text:
        if output_path:
            output_path.write_text(text, encoding="utf-8")
            _console.print(f"  [green]✓[/] Output saved to [cyan]{output_path}[/]")
        else:
            _console.print(text)

    # Exit code reflects severity
    if report.critical_count > 0:
        raise typer.Exit(code=2)


@app.command(help="Review a diff or commit (default mode for git changes).")
def review(
    path: Path = typer.Argument(Path.cwd(), help="Repository path."),
    commit: str = typer.Option("HEAD", "--commit", "-c", help="Commit to review."),
    output_format: str = typer.Option("stream", "--format", "-f", help="stream | json | markdown"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to file."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="auto | js | plc | python"),
    budget: Optional[int] = typer.Option(None, "--budget", "-b", help="Max tool calls."),
):
    _run("review", path, commit, output_format, output, profile, budget)


@app.command(help="Full-repo security audit.")
def audit(
    path: Path = typer.Argument(Path.cwd(), help="Repository path."),
    output_format: str = typer.Option("stream", "--format", "-f", help="stream | json | markdown"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to file."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="auto | js | plc | python"),
    budget: Optional[int] = typer.Option(None, "--budget", "-b", help="Max tool calls."),
):
    _run("audit", path, "", output_format, output, profile, budget)


@app.command(help="Find AI-generated redundancy (dedup mode).")
def dedup(
    path: Path = typer.Argument(Path.cwd(), help="Repository path."),
    output_format: str = typer.Option("stream", "--format", "-f", help="stream | json | markdown"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to file."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="auto | js | plc | python"),
    budget: Optional[int] = typer.Option(None, "--budget", "-b", help="Max tool calls."),
    fix: bool = typer.Option(False, "--fix", help="Apply proposed patches interactively."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --fix: show patches but don't apply."),
    yes: bool = typer.Option(False, "--yes", help="With --fix: auto-approve high-confidence patches (CI mode)."),
    min_confidence: float = typer.Option(0.95, "--min-confidence", help="Min confidence for --yes auto-approve."),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="With --fix: allow apply on dirty git repo (stashes first)."),
):
    """Run dedup mode, then optionally apply the proposed patches."""
    _run_dedup_with_fix(
        path=path,
        output_format=output_format,
        output=output,
        profile=profile,
        budget=budget,
        fix=fix,
        dry_run=dry_run,
        yes=yes,
        min_confidence=min_confidence,
        allow_dirty=allow_dirty,
    )


def _run_dedup_with_fix(
    *,
    path: Path,
    output_format: str,
    output: Optional[Path],
    profile: Optional[str],
    budget: Optional[int],
    fix: bool,
    dry_run: bool,
    yes: bool,
    min_confidence: float,
    allow_dirty: bool,
):
    """Dedup + optional interactive patch application."""
    cfg = _ensure_config()
    repo_path = path.expanduser().resolve()
    if not repo_path.is_dir():
        _err_console.print(f"  ✗ Path does not exist: {repo_path}")
        raise typer.Exit(code=1)

    if budget is not None:
        cfg = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_tool_calls": budget})})

    profile_to_use: str = profile or cfg.profile.default

    from ..output.stream import StreamRenderer, format_as_json, format_as_markdown
    from ..agent import run_agent_sync

    renderer = StreamRenderer(_console) if output_format == "stream" else StreamRenderer(_console, verbose=False)
    on_event = renderer.handle if output_format == "stream" else (lambda e, p: None)

    # We need access to the final state's `patches` field for --fix, so we
    # use a capture closure instead of relying on the returned report.
    captured_patches: list = []

    def capture_session_end(event: str, payload: dict):
        on_event(event, payload)
        if event == "session_end":
            report = payload.get("report") or {}
            # Patches live in the snapshot state. Pull from the runner-provided
            # state. Since run_agent already serialized them via model_dump,
            # the report carries no patches field — we'll re-fetch from the
            # checkpoint at the end. For now, the report only has findings.

    try:
        report = run_agent_sync(
            mode="dedup",
            repo_path=str(repo_path),
            target_ref="",
            profile_name=profile_to_use,
            config=cfg,
            on_event=capture_session_end,
        )
    except KeyboardInterrupt:
        _console.print("\n[yellow]Interrupted by user.[/]")
        raise typer.Exit(code=130)
    except Exception as e:
        _err_console.print(f"\n  ✗ Agent failed: {e}")
        if os.environ.get("REVIO_DEBUG"):
            import traceback
            traceback.print_exc()
        raise typer.Exit(code=1)

    # Non-stream output formats — print the report
    if output_format == "json":
        text = format_as_json(report)
    elif output_format == "markdown":
        text = format_as_markdown(report)
    else:
        text = ""
    if text:
        if output:
            output.write_text(text, encoding="utf-8")
            _console.print(f"  [green]✓[/] Output saved to [cyan]{output}[/]")
        else:
            _console.print(text)

    # --- Fix flow ----------------------------------------------------------
    if fix or dry_run:
        # Pull patches from the agent's final state. The runner cached them
        # in the SQLite checkpoint; the simplest path is to re-fetch via a
        # helper that reads the last session's patches list. For M4, we
        # bypass the checkpoint round-trip by collecting patches from the
        # captured stream events instead.
        patches = _pull_patches_from_recent_run(cfg, repo_path)
        if not patches:
            _console.print()
            _console.print("  [yellow]·[/] No patches proposed by the agent. Nothing to apply.")
            _console.print("      The agent only generates patches when it identifies clearly")
            _console.print("      mechanical fixes (typically dedup candidates).")
            return

        from .fix import run_fix_flow

        result = run_fix_flow(
            patches=patches,
            repo_root=repo_path,
            dry_run=dry_run,
            yes=yes,
            min_confidence=min_confidence,
            allow_dirty=allow_dirty,
            console=_console,
        )

        if result.failed and not result.applied:
            raise typer.Exit(code=1)

    if report.critical_count > 0:
        raise typer.Exit(code=2)


def _pull_patches_from_recent_run(cfg, repo_path: Path) -> list:
    """Return patches the runner stashed on the module-level cache.

    The runner (revio.agent.runner.run_agent) writes the final state's
    `patches` list into `_last_session_patches` at session_end. Read via
    sys.modules to defeat the `revio.cli.main` name-collision (the function
    shadows the module attribute).
    """
    import sys
    this_module = sys.modules[__name__]
    cached = getattr(this_module, "_last_session_patches", [])
    return list(cached) if cached else []


# Module-level cache populated by the runner when --fix-aware mode runs.
# Set by a future patch in runner.py that copies state.values["patches"]
# into here at session_end. For M4 we accept this as a simple bridge.
_last_session_patches: list = []


# --- config subcommands -------------------------------------------------------


@config_app.command("init", help="Run the setup wizard.")
def config_init():
    from .wizard import run_wizard

    cfg = run_wizard()
    if cfg is None:
        raise typer.Exit(code=1)


@config_app.command("show", help="Print the current merged config (api_key masked).")
def config_show():
    cfg = load_config(Path.cwd())
    data = cfg.model_dump()
    # Mask API key
    if data.get("llm", {}).get("api_key"):
        k = data["llm"]["api_key"]
        data["llm"]["api_key"] = (k[:6] + "…" + k[-4:]) if len(k) > 10 else "…"
    _console.print_json(data=data)


@config_app.command("path", help="Print the user-global config file path.")
def config_path():
    typer.echo(str(user_config_path()))


@config_app.command("edit", help="Open the user-global config in $EDITOR.")
def config_edit():
    path = user_config_path()
    if not path.is_file():
        _err_console.print(f"  ✗ Config does not exist yet. Run [bold]revio config init[/] first.")
        raise typer.Exit(code=1)
    editor = os.environ.get("EDITOR", "nano")
    os.system(f'{editor} "{path}"')


# --- guidelines subcommands ---------------------------------------------------


def _resolve_repo_root() -> Path:
    """Pick the repo root for guidelines storage (current dir for now)."""
    return Path.cwd().resolve()


@guidelines_app.command("add", help="Index one or more guideline files / directories.")
def guidelines_add(
    paths: list[Path] = typer.Argument(..., help="Files or directories to index."),
):
    from ..layers.rag import DocumentLoader, GuidelineIndexer

    repo = _resolve_repo_root()
    indexer = GuidelineIndexer(repo_root=repo)
    total_chunks = 0
    total_files = 0

    for raw_path in paths:
        p = Path(raw_path).expanduser().resolve()
        if not p.exists():
            _err_console.print(f"  ✗ not found: {p}")
            continue

        if p.is_dir():
            docs = DocumentLoader.load_directory(p)
        else:
            docs = DocumentLoader.load_file(p)

        if not docs:
            _console.print(f"  [yellow]·[/] no chunks extracted from {p}")
            continue

        n = indexer.index_documents(docs)
        total_chunks += n
        # Distinct file count from metadata
        distinct_sources = {d.metadata.get("source") for d in docs}
        total_files += len(distinct_sources)
        _console.print(f"  [green]✓[/] indexed {n} chunks from {p}")

    _console.print()
    _console.print(
        f"  [bold]Total:[/] {total_chunks} chunks from {total_files} files. "
        f"Index now has {indexer.count()} chunks."
    )


@guidelines_app.command("list", help="List all indexed guideline files.")
def guidelines_list():
    from ..layers.rag import GuidelineIndexer

    repo = _resolve_repo_root()
    indexer = GuidelineIndexer(repo_root=repo)
    sources = indexer.list_sources()
    count = indexer.count()

    if not sources:
        _console.print("  [dim](no guidelines indexed for this repo)[/]")
        _console.print(f"  Add some with: [bold]revio guidelines add <file_or_dir>[/]")
        return

    _console.print(f"  [bold]{len(sources)} files[/] indexed ({count} chunks total):")
    for src in sources:
        _console.print(f"    · {src}")


@guidelines_app.command("clear", help="Remove all guideline chunks from the index.")
def guidelines_clear():
    from ..layers.rag import GuidelineIndexer

    if not questionary.confirm(
        "This will drop the entire guideline index for this repo. Continue?",
        default=False,
    ).ask():
        _console.print("  [dim]cancelled[/]")
        return

    repo = _resolve_repo_root()
    indexer = GuidelineIndexer(repo_root=repo)
    indexer.clear()
    _console.print("  [green]✓[/] index cleared")


@guidelines_app.command("reindex", help="Drop and rebuild the index from .revio/guidelines/.")
def guidelines_reindex():
    from ..layers.rag import DocumentLoader, GuidelineIndexer

    repo = _resolve_repo_root()
    guidelines_dir = repo / ".revio" / "guidelines"
    if not guidelines_dir.is_dir():
        _err_console.print(f"  ✗ no guidelines dir at {guidelines_dir}")
        _console.print(
            "  Create it and drop your guideline files there, then rerun. "
            "Or use [bold]revio guidelines add <path>[/] for arbitrary paths."
        )
        raise typer.Exit(code=1)

    indexer = GuidelineIndexer(repo_root=repo)
    indexer.clear()
    docs = DocumentLoader.load_directory(guidelines_dir)
    n = indexer.index_documents(docs)
    _console.print(f"  [green]✓[/] reindexed: {n} chunks from {guidelines_dir}")


@guidelines_app.command("search", help="Test the RAG retrieval with a query.")
def guidelines_search(
    query: str = typer.Argument(..., help="Query text."),
    k: int = typer.Option(5, "--k", help="Number of results."),
):
    from ..layers.rag import GuidelineRetriever

    repo = _resolve_repo_root()
    retriever = GuidelineRetriever(repo_root=repo)
    if not retriever.has_index():
        _err_console.print("  ✗ no guidelines indexed for this repo. Run `revio guidelines add` first.")
        raise typer.Exit(code=1)

    results = retriever.search_with_scores(query, k=k)
    if not results:
        _console.print(f"  [yellow]·[/] no matches for {query!r}")
        return

    _console.print(f"  [bold]{len(results)} results[/] for {query!r}:\n")
    for doc, score in results:
        src = Path(doc.metadata.get("source", "?")).name
        section = doc.metadata.get("section_title", "")
        location = f"{src}" + (f" / {section}" if section else "")
        body = doc.page_content.strip()
        if len(body) > 300:
            body = body[:300] + "..."
        _console.print(f"  [cyan]{location}[/]  [dim](relevance={score:.2f})[/]")
        for ln in body.splitlines():
            _console.print(f"    {ln}")
        _console.print()


# --- skills subcommands -------------------------------------------------------


@skills_app.command("list", help="List all discovered skills.")
def skills_list():
    from ..skills import SkillsRegistry, project_skills_dir, user_skills_dir

    reg = SkillsRegistry.discover(project_root=Path.cwd())
    skills = reg.all()

    _console.print()
    _console.print(f"  [dim]project skills dir:[/] {project_skills_dir()}")
    _console.print(f"  [dim]user skills dir:[/] {user_skills_dir()}")
    _console.print()

    if not skills:
        _console.print("  [yellow]·[/] no skills discovered.")
        _console.print(
            "  Create a skill at [bold].revio/skills/<name>/SKILL.md[/] "
            "with YAML frontmatter (name, description, when_to_use)."
        )
        return

    _console.print(f"  [bold]{len(skills)} skills[/] discovered:")
    for s in skills:
        source_tag = (
            "[green][project][/]" if s.source == "project" else "[blue][user][/]"
        )
        _console.print(f"    {source_tag} [bold]{s.name}[/]: {s.description}")


@skills_app.command("show", help="Print a skill's full body to stdout.")
def skills_show(name: str = typer.Argument(..., help="Skill name.")):
    from ..skills import SkillsRegistry

    reg = SkillsRegistry.discover(project_root=Path.cwd())
    skill = reg.get(name)
    if skill is None:
        _err_console.print(f"  ✗ no skill named [bold]{name}[/]")
        raise typer.Exit(code=1)

    _console.print(f"  [bold]{skill.name}[/]  [dim]({skill.source})[/]")
    _console.print(f"  [dim]source:[/] {skill.body_path}")
    _console.print(f"  [dim]description:[/] {skill.description}")
    if skill.when_to_use:
        _console.print(f"  [dim]when_to_use:[/] {skill.when_to_use}")

    rules = skill.matches
    rule_parts = []
    if rules.extensions:
        rule_parts.append(f"ext={rules.extensions}")
    if rules.imports:
        rule_parts.append(f"imports={rules.imports}")
    if rules.frameworks:
        rule_parts.append(f"frameworks={rules.frameworks}")
    if rules.languages:
        rule_parts.append(f"languages={rules.languages}")
    if rules.filename_patterns:
        rule_parts.append(f"filenames={rules.filename_patterns}")
    if rule_parts:
        _console.print(f"  [dim]matches:[/] {' '.join(rule_parts)}")

    _console.print()
    _console.print(skill.load_body())


@skills_app.command("activated", help="Show which skills would auto-activate for the current dir.")
def skills_activated():
    from ..agent.tool_context import ToolContext

    ctx = ToolContext(repo_root=Path.cwd(), profile_name="auto")
    activations = ctx.activated_skills

    if not activations:
        _console.print("  [yellow]·[/] no skills auto-activate for this project")
        return

    _console.print(f"  [bold]{len(activations)} skills would auto-activate:[/]")
    for act in activations:
        _console.print(f"    [bold]{act.skill.name}[/] — matched: {', '.join(act.matched_rules)}")


# --- MCP server subcommand ----------------------------------------------------


@app.command(
    "mcp-server",
    help=(
        "Start revio as an MCP server (stdio). External agents (Claude Code, "
        "Cursor, etc.) can then call revio_audit / revio_dedup / revio_review "
        "and individual static analyzers. Logs go to stderr; stdout is the "
        "JSON-RPC channel."
    ),
)
def mcp_server():
    from ..mcp_server import run_stdio

    run_stdio()


# --- main ---------------------------------------------------------------------


def main():
    app()


if __name__ == "__main__":
    main()
