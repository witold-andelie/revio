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


# --- main ---------------------------------------------------------------------


def main():
    app()


if __name__ == "__main__":
    main()
