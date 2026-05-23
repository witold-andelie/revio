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
    fix: bool = typer.Option(False, "--fix", help="Apply changes (M1: not yet implemented)."),
):
    if fix:
        _err_console.print("  ⚠ --fix is not implemented in M1 — running in dry-run mode.")
    _run("dedup", path, "", output_format, output, profile, budget)


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


# --- main ---------------------------------------------------------------------


def main():
    app()


if __name__ == "__main__":
    main()
