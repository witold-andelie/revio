"""Interactive REPL with slash commands and natural-language intent.

UX patterns:
- Slash commands (`/help`, `/model ...`) match traditional CLI shortcuts
- Anything else is natural language → LLM-based intent classifier → routes to mode
- Natural language input may be in ANY human language (en, zh, de, fr, es, ja, cs, ...).
  The classifier is multilingual by design; the agent's response stays English.
- Tab completion for slash commands + paths
- Persistent history at ~/.cache/revio/repl_history
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import questionary
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel

from ..config import Config, load_config, save_user_config, user_config_path
from ..output.stream import StreamRenderer


_console = Console()


# --- Slash command registry ---------------------------------------------------


SLASH_COMMANDS = {
    "/help":     "List all slash commands",
    "/?":        "Alias for /help",
    "/model":    "Pick or change LLM model (`/model` to browse, `/model list`, `/model <name>`)",
    "/models":   "Alias for `/model list` — show all available models on current endpoint",
    "/url":      "Change API endpoint (/url to change interactively — re-keys + re-detects models; or /url <url>)",
    "/key":      "Update API key (masked input)",
    "/profile":  "Switch language profile (auto / js / plc / python)",
    "/mode":     "Default mode for next NL input (review / audit / dedup)",
    "/budget":   "Set tool-call budget for this session (usage: /budget 25)",
    "/cost":     "Show estimated tokens used in this session",
    "/config":   "Open config file in $EDITOR",
    "/clear":    "Clear screen",
    "/history":  "Show this REPL's command history",
    "/exit":     "Exit revio",
    "/quit":     "Alias for /exit",
}


# --- Multilingual intent classifier prompt -----------------------------------


_INTENT_SYSTEM = """You are the intent router for the revio code-review CLI.

The user typed a free-form request. revio can do a fixed set of things — pick
the ONE intent that matches, or flag the request as out of scope.

- review       : review a diff or a specific commit
- audit        : full-repo security / quality audit (no diff context)
- dedup        : find AND clean AI-generated / "vibe-coding" redundancy —
                 duplicate functions, dead code, no-op wrappers, junk code
                 (e.g. "clean up the junk code", "去掉重复/废物代码", "删掉死代码")
- config       : change or show a revio SETTING — the LLM model, the API
                 endpoint URL, the API key, the language profile, the default
                 mode, or the tool-call budget; or show the config / session cost
- capability   : the user is asking what revio can do / for help
- out_of_scope : the request is OUTSIDE revio's abilities. revio ONLY reviews
                 code and manages its own settings. It does NOT write new
                 features, edit business logic, run arbitrary shell commands,
                 deploy, browse the web, or answer general/unrelated questions.

The user may type in ANY human language (en / 中文 / Deutsch / français /
español / 日本語 / …). Classify regardless of language.

For `config`, ALSO emit a `slash` field — the exact revio slash command that
performs the change, chosen ONLY from this whitelist (never invent others):
  /model <name>         set the model
  /model list           show available models
  /url <https-url>      set the API endpoint URL
  /key                  update the API key  (SECURITY: never put the key value
                        in `slash`; the CLI prompts for it securely. Always emit
                        exactly "/key".)
  /profile <auto|js|plc|python>
  /mode <review|audit|dedup>
  /budget <1-200>
  /cost                 show session cost
  /config               show / open the config file

For review / audit / dedup also extract:
- target_path : relative or absolute filesystem path mentioned (string or null).
                This MAY be a single FILE (e.g. "src/auth.py", "C:\\app\\db.go")
                — extract it precisely so that one file can be scanned on its own,
                not just a directory.
- target_ref  : a git commit, branch, or "HEAD" if mentioned (string or null)
- focus_area  : security / performance / readability / etc. (string or null)

Respond with strict JSON in this shape:
{
  "intent": "review" | "audit" | "dedup" | "config" | "capability" | "out_of_scope",
  "slash": null,
  "target_path": null,
  "target_ref": null,
  "focus_area": null,
  "rationale": "one English sentence why"
}
"""


# --- REPL completer -----------------------------------------------------------


class _ReplCompleter(Completer):
    """Tab-completes slash commands and file paths."""

    def __init__(self):
        self._path = PathCompleter(expanduser=True)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Slash command: filter by the FULL "/..." token typed so far, so you
        # can keep typing letters (/u → /url) and still get a live, narrowing
        # dropdown — not just scroll the whole list. WordCompleter used to be
        # used here, but it splits words on "/", so typing after the slash
        # extracted a slash-less word that matched nothing and cleared the menu.
        if text.startswith("/") and " " not in text:
            for cmd, desc in SLASH_COMMANDS.items():
                if cmd.startswith(text.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(text),  # replace the whole "/foo" token
                        display=cmd,
                        display_meta=desc,
                    )
            return
        # Best-effort path completion when looking at a path-shaped token
        last = text.split()[-1] if text.split() else ""
        if last.startswith("/") or last.startswith("~") or last.startswith("."):
            yield from self._path.get_completions(document, complete_event)


# --- REPL session -------------------------------------------------------------


def _history_path() -> Path:
    p = Path.home() / ".cache" / "revio" / "repl_history"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _trim_history_file(path: Path, max_entries: int) -> None:
    """Keep only the most recent `max_entries` commands in the REPL history.

    prompt_toolkit's FileHistory stores each entry as a `# <timestamp>` line
    followed by `+`-prefixed content lines, entries separated by blank lines.
    Count-based, oldest dropped; best-effort (never blocks REPL startup).
    """
    try:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Split on each entry's leading "# " timestamp comment.
        entries = [e for e in re.split(r"\n(?=# )", text.strip("\n")) if e.strip()]
        if len(entries) <= max_entries:
            return
        path.write_text("\n".join(entries[-max_entries:]) + "\n", encoding="utf-8")
    except Exception:
        pass


_PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "ansicyan bold",
        "mode": "ansigreen",
        "rmodel": "ansibrightblack",
    }
)


def _prompt_text(cfg: Config, mode: str) -> HTML:
    short_model = cfg.llm.model.split("-")[1] if "-" in cfg.llm.model else cfg.llm.model
    return HTML(
        f"<rmodel>[{short_model}]</rmodel> <mode>({mode})</mode> <prompt>›</prompt> "
    )


def run_repl():
    """Drop into the interactive REPL."""
    cfg = load_config(Path.cwd())

    # Session-local state (not persisted)
    state = {
        "mode": "review",
        "profile": cfg.profile.default,
        "budget": cfg.agent.max_tool_calls,
        "cwd": Path.cwd().resolve(),
        # Token accounting across all NL queries in this REPL session
        "session_tokens_in": 0,
        "session_tokens_out": 0,
        "session_cost_usd": 0.0,
        "session_llm_calls": 0,
    }

    # Count-based cleanup of the REPL history file before we open it.
    _trim_history_file(_history_path(), cfg.memory.repl_history_max_entries)

    session = PromptSession(
        history=FileHistory(str(_history_path())),
        auto_suggest=AutoSuggestFromHistory(),
        completer=_ReplCompleter(),
        complete_while_typing=True,  # live dropdown as you type "/..." (Claude-Code-like)
        style=_PROMPT_STYLE,
    )

    _print_banner(cfg, state)

    while True:
        try:
            raw = session.prompt(_prompt_text(cfg, state["mode"]))
        except (KeyboardInterrupt, EOFError):
            _console.print("\n[dim]goodbye[/]\n")
            return

        line = raw.strip()
        if not line:
            continue

        # Slash commands are always single-line; a multi-line paste starting
        # with "/" is treated as content, not a command.
        if line.startswith("/") and "\n" not in line:
            keep_going = _handle_slash(line, cfg, state)
            if not keep_going:
                return
            continue

        # Pasted code snippet — review the code inline (no file path needed).
        snippet = _extract_snippet(line)
        if snippet:
            _handle_snippet_input(*snippet, cfg, state)
            continue

        # Natural language — classify, route, execute
        _handle_nl_input(line, cfg, state)


def _print_banner(cfg: Config, state: dict) -> None:
    from .mascot import play_startup_animation

    _console.print()
    play_startup_animation(_console)
    _console.print()
    _console.print(
        Panel(
            "[bold cyan]revio[/]  — agentic code review\n\n"
            f"  model    [dim]{cfg.llm.model}[/]\n"
            f"  profile  [dim]{state['profile']}[/]\n"
            f"  cwd      [dim]{state['cwd']}[/]\n\n"
            "Type a request in any language, or use [bold]/help[/] for commands.\n"
            "Examples:\n"
            "  • [dim]review the last commit[/]\n"
            "  • [dim]audit src/auth for security issues[/]\n"
            "  • [dim]check this file: src/auth.py[/]   [dim](scans just that file)[/]\n"
            "  • [dim]paste code (optionally in ```fences```) to review it inline[/]\n"
            "  • [dim]clean up the duplicate / junk code[/]\n"
            "  • [dim]switch the model to claude-opus-4-7[/]\n"
            "  • [dim]set my api key[/]   ·   [dim]what can you do?[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    _console.print()


# --- Slash dispatcher ---------------------------------------------------------


def _handle_slash(line: str, cfg: Config, state: dict) -> bool:
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    handlers: dict[str, Callable[[str, Config, dict], bool]] = {
        "/help": _cmd_help,
        "/?": _cmd_help,
        "/model": _cmd_model,
        "/models": lambda arg, cfg, state: _cmd_model("list", cfg, state),
        "/url": _cmd_url,
        "/key": _cmd_key,
        "/profile": _cmd_profile,
        "/mode": _cmd_mode,
        "/budget": _cmd_budget,
        "/cost": _cmd_cost,
        "/config": _cmd_config_edit,
        "/clear": _cmd_clear,
        "/history": _cmd_history,
        "/exit": _cmd_exit,
        "/quit": _cmd_exit,
    }
    handler = handlers.get(cmd)
    if handler is None:
        _console.print(f"  [yellow]unknown command:[/] {cmd}   (try /help)")
        return True
    return handler(arg, cfg, state)


def _cmd_help(arg: str, cfg: Config, state: dict) -> bool:
    _console.print()
    _console.print("[bold]Slash commands[/]")
    for cmd, desc in SLASH_COMMANDS.items():
        _console.print(f"  [cyan]{cmd:<10}[/] {desc}")
    _console.print(
        "\nAnything not starting with [cyan]/[/] is treated as a natural-language\n"
        "request: review / audit / dedup, OR a settings change (\"switch the\n"
        "model to ...\", \"set my api key\", \"budget 30\"). Ask \"what can you do?\"\n"
        "for the full list. Out-of-scope requests are flagged. Multilingual OK.\n"
    )
    return True


def _cmd_model(arg: str, cfg: Config, state: dict) -> bool:
    """`/model`                — interactive picker (live + curated)
    `/model list`              — show available, no picker
    `/model <name>`            — set directly (no validation)
    """
    from .models_catalog import format_table, list_models_for

    arg = arg.strip()

    # Subcommand: list — print the catalog and return
    if arg == "list" or arg == "ls":
        _console.print(f"  [dim]endpoint:[/] {cfg.llm.api_url}")
        entries = list_models_for(cfg.llm.api_url, cfg.llm.resolve_api_key())
        for line in format_table(entries, current=cfg.llm.model):
            _console.print(line)
        if entries:
            _console.print("  [dim]live[/] = discovered via /v1/models. "
                           "Use [bold]/model <name>[/] or just [bold]/model[/] to pick.")
        return True

    # Direct set: `/model deepseek-reasoner`
    if arg:
        cfg.llm.model = arg
        save_user_config(cfg)
        _console.print(f"  [green]✓[/] model → [cyan]{arg}[/]")
        return True

    # No arg → interactive picker
    _console.print(f"  current model: [cyan]{cfg.llm.model}[/]")
    _console.print(f"  [dim]endpoint:[/] {cfg.llm.api_url}")
    _console.print("  [dim]discovering models...[/]")
    entries = list_models_for(cfg.llm.api_url, cfg.llm.resolve_api_key())
    if not entries:
        _console.print("  [yellow]no catalog available; type [bold]/model <name>[/] to set manually[/]")
        return True

    choices = []
    for e in entries:
        label = e.name
        if e.note:
            label += f"  ({e.note})"
        if e.source == "discovered":
            label += "  [live]"
        choices.append(questionary.Choice(label, value=e.name))
    choices.append(questionary.Choice("(cancel)", value=None))

    picked = questionary.select(
        "Pick a model:",
        choices=choices,
        default=next((c for c in choices if c.value == cfg.llm.model), choices[0]),
    ).ask()
    if not picked:
        _console.print("  [dim]cancelled[/]")
        return True
    cfg.llm.model = picked
    save_user_config(cfg)
    _console.print(f"  [green]✓[/] model → [cyan]{picked}[/]")
    return True


def _infer_provider(url: str) -> str:
    """Guess the protocol family from an endpoint URL.

    Anthropic Messages API for the official host or an `/anthropic`-suffixed
    gateway (e.g. Mimo); everything else (DeepSeek, Mistral, OpenRouter,
    OpenAI, Together, vLLM, Ollama, LM Studio, ...) speaks OpenAI-compatible.
    """
    u = url.lower().rstrip("/")
    if "anthropic.com" in u or u.endswith("/anthropic"):
        return "anthropic"
    return "openai_compat"


def _apply_url_change(new_url: str, cfg: Config, state: dict) -> None:
    """Switch the endpoint, auto-match its protocol, then re-key + re-pick model.

    After this the agent connects to the new API automatically (correct
    protocol) and the model is chosen from what that endpoint actually serves —
    the user never has to edit the config file by hand.
    """
    new_url = new_url.strip()
    prev_provider = cfg.llm.provider
    cfg.llm.api_url = new_url
    cfg.llm.provider = _infer_provider(new_url)  # type: ignore[assignment]
    save_user_config(cfg)
    _console.print(
        f"  [green]✓[/] endpoint → [cyan]{new_url}[/]  "
        f"[dim](protocol auto-set: {cfg.llm.provider})[/]"
    )

    # A different endpoint almost always needs its own key — offer to set it.
    if questionary.confirm(
        "Update the API key for this endpoint now?",
        default=(cfg.llm.provider != prev_provider),
    ).ask():
        _cmd_key("", cfg, state)

    # Auto-discover the models this endpoint serves and let the user pick one.
    # Reuses /model's live discovery — no config-file editing needed.
    _console.print("  [dim]matching available models on the new endpoint...[/]")
    _cmd_model("", cfg, state)


def _cmd_url(arg: str, cfg: Config, state: dict) -> bool:
    arg = arg.strip()
    # Explicit `/url <url>` keeps the direct path (also used by the NL router).
    if arg:
        if not arg.startswith(("http://", "https://")):
            _console.print("  [red]✗[/] URL must start with http:// or https://")
            return True
        _apply_url_change(arg, cfg, state)
        return True

    # Bare `/url` → show current endpoint, then offer to change it interactively.
    _console.print(
        f"  current endpoint: [cyan]{cfg.llm.api_url}[/]  "
        f"[dim](protocol: {cfg.llm.provider})[/]"
    )
    if not questionary.confirm("Change the API endpoint URL?", default=False).ask():
        _console.print("  [dim]unchanged[/]")
        return True
    new_url = questionary.text(
        "New API URL:",
        default=cfg.llm.api_url,
        validate=lambda x: True
        if x.strip().startswith(("http://", "https://"))
        else "URL must start with http:// or https://",
    ).ask()
    if not new_url:
        _console.print("  [dim]cancelled[/]")
        return True
    _apply_url_change(new_url, cfg, state)
    return True


def _cmd_key(arg: str, cfg: Config, state: dict) -> bool:
    new_key = questionary.password("New API key:").ask()
    if not new_key:
        _console.print("  [dim]cancelled[/]")
        return True
    cfg.llm.api_key = new_key.strip()
    save_user_config(cfg)
    _console.print(f"  [green]✓[/] api_key updated")
    return True


def _cmd_profile(arg: str, cfg: Config, state: dict) -> bool:
    if not arg:
        _console.print(f"  current profile: [cyan]{state['profile']}[/]")
        return True
    if arg not in {"auto", "js", "plc", "python"}:
        _console.print("  [red]✗[/] profile must be one of: auto, js, plc, python")
        return True
    state["profile"] = arg
    _console.print(f"  [green]✓[/] profile → [cyan]{arg}[/] (session-only)")
    return True


def _cmd_mode(arg: str, cfg: Config, state: dict) -> bool:
    if not arg:
        _console.print(f"  default mode: [cyan]{state['mode']}[/]")
        return True
    if arg not in {"review", "audit", "dedup"}:
        _console.print("  [red]✗[/] mode must be one of: review, audit, dedup")
        return True
    state["mode"] = arg
    _console.print(f"  [green]✓[/] mode → [cyan]{arg}[/]")
    return True


def _cmd_budget(arg: str, cfg: Config, state: dict) -> bool:
    if not arg:
        _console.print(f"  current budget: [cyan]{state['budget']}[/] tool calls")
        return True
    try:
        n = int(arg)
        if n < 1 or n > 200:
            raise ValueError
    except ValueError:
        _console.print("  [red]✗[/] budget must be an integer 1-200")
        return True
    state["budget"] = n
    _console.print(f"  [green]✓[/] budget → [cyan]{n}[/]")
    return True


def _cmd_cost(arg: str, cfg: Config, state: dict) -> bool:
    from ..output.cost import format_count, format_cost, is_priced

    tin = state.get("session_tokens_in", 0)
    tout = state.get("session_tokens_out", 0)
    cost = state.get("session_cost_usd", 0.0)
    calls = state.get("session_llm_calls", 0)
    if tin == 0 and tout == 0:
        _console.print("  [dim](no LLM calls yet this REPL session)[/]")
        return True
    _console.print(f"  [dim]model[/]      {cfg.llm.model}")
    _console.print(f"  [dim]LLM calls[/]  {calls}")
    _console.print(f"  [dim]input[/]      {format_count(tin)} tokens")
    _console.print(f"  [dim]output[/]     {format_count(tout)} tokens")
    if is_priced(cfg.llm.model):
        _console.print(f"  [dim]est. cost[/]  [bold]{format_cost(cost)}[/]")
    return True


def _cmd_config_edit(arg: str, cfg: Config, state: dict) -> bool:
    path = user_config_path()
    editor = os.environ.get("EDITOR", "nano")
    # Launch the editor WITHOUT a shell so a hostile $EDITOR can't inject
    # commands. shlex.split preserves multi-word editors like "code --wait".
    try:
        subprocess.run([*shlex.split(editor, posix=(os.name != "nt")), str(path)])
    except FileNotFoundError:
        _console.print(f"  [red]✗[/] editor not found: {editor!r} (check $EDITOR)")
        return True
    # Reload after edit
    cfg2 = load_config(state["cwd"])
    cfg.__dict__.update(cfg2.__dict__)
    _console.print("  [green]✓[/] config reloaded")
    return True


def _cmd_clear(arg: str, cfg: Config, state: dict) -> bool:
    _console.clear()
    return True


def _cmd_history(arg: str, cfg: Config, state: dict) -> bool:
    p = _history_path()
    if not p.is_file():
        _console.print("  [dim](no history yet)[/]")
        return True
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
    for line in lines:
        # FileHistory stores prefixed with "+" — strip if present
        text = line.lstrip("+").strip()
        if text:
            _console.print(f"  [dim]·[/] {text}")
    return True


def _cmd_exit(arg: str, cfg: Config, state: dict) -> bool:
    _console.print("\n[dim]goodbye[/]\n")
    return False


# --- Natural language handler -------------------------------------------------


# The full set of intents the router understands.
_VALID_INTENTS = {"review", "audit", "dedup", "config", "capability", "out_of_scope", "chat"}

# What revio can actually do — the single source of truth for the capability
# summary AND the out-of-scope reminder. (English-only, per the project's
# English-only-UI convention; see docs/INTERNALS.md §13f.)
CAPABILITIES = [
    ("Review a diff or commit",
     "review the last commit · 看看这次改动"),
    ("Scan a single file, or code you paste straight into the chat",
     "check this file: src/auth.py · paste code in ```fences``` to review it"),
    ("Audit the whole repo for security & quality issues",
     "audit src/ for vulnerabilities · 扫描漏洞"),
    ("Find & clean AI-generated / vibe-coding redundancy (dupes, dead code)",
     "clean up the junk/duplicate code · 去掉重复的废代码"),
    ("Change a setting: model, API endpoint, API key, profile, mode, budget",
     "switch to claude-opus-4-7 · set my api key · budget 30"),
    ("Show the current config or this session's cost",
     "show my config · how much did this cost"),
]

# NL config requests are only ever mapped to one of these slash commands. The
# router (LLM or keyword fallback) proposes a slash string; we refuse anything
# outside this set so a misclassification can never run an arbitrary command.
_ALLOWED_SLASH_FROM_NL = {
    "/model", "/url", "/key", "/profile", "/mode", "/budget", "/cost", "/config",
}


def _print_capabilities(reason: str = "") -> None:
    """Show what revio can do (for `capability` / help-style requests)."""
    _console.print()
    _console.print("  [bold]Here's what I can do for you in plain language:[/]")
    for what, example in CAPABILITIES:
        _console.print(f"    [cyan]•[/] {what}")
        _console.print(f"        [dim]e.g. {example}[/]")
    _console.print(
        "\n  Slash commands ([cyan]/help[/]) do the same things as shortcuts."
    )
    _console.print()


def _print_out_of_scope(rationale: str = "") -> None:
    """Tell the user their request is beyond revio's capability boundary."""
    _console.print()
    _console.print(
        "  [yellow]⚠ That request is outside what revio can do.[/]"
    )
    if rationale:
        _console.print(f"  [dim]{rationale}[/]")
    _console.print(
        "  revio is a code-review agent — it reviews/audits code, cleans\n"
        "  AI-generated redundancy, and manages its own settings. It does not\n"
        "  write features, run arbitrary commands, deploy, or answer general\n"
        "  questions."
    )
    _print_capabilities()


def _handle_config_intent(classification: dict, cfg: Config, state: dict) -> None:
    """Apply an NL-driven settings change by reusing the slash dispatcher.

    The classifier proposes a slash command; we validate it against the
    whitelist, force the API-key path to be interactive (never accept a secret
    from free text), confirm endpoint changes, then hand off to _handle_slash.
    """
    slash = (classification.get("slash") or "").strip()
    if not slash.startswith("/"):
        _console.print(
            "  [yellow]I understood you want to change a setting, but couldn't\n"
            "  map it to a command. Try [bold]/help[/] for the exact options.[/]"
        )
        return

    cmd = slash.split(maxsplit=1)[0].lower()
    if cmd not in _ALLOWED_SLASH_FROM_NL:
        _console.print(
            f"  [yellow]'{cmd}' isn't a setting revio can change from natural\n"
            f"  language. Try [bold]/help[/].[/]"
        )
        return

    # SECURITY: never take an API key from free-text input — drop any value the
    # classifier may have attached and force the masked interactive prompt.
    if cmd == "/key":
        slash = "/key"
    elif cmd == "/url" and " " in slash:
        new_url = slash.split(maxsplit=1)[1].strip()
        if not questionary.confirm(
            f"Change the API endpoint to {new_url}?", default=False
        ).ask():
            _console.print("  [dim]cancelled[/]")
            return

    _console.print(f"  [dim]· interpreting that as[/] [cyan]{slash}[/]")
    _handle_slash(slash, cfg, state)


def _git_root_of(path: Path) -> Path | None:
    """Return the git top-level dir containing `path`, or None if not in a repo."""
    start = path if path.is_dir() else path.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    root = out.stdout.strip()
    return Path(root) if root else None


def _run_agent_on(
    *,
    mode: str,
    repo_path: Path,
    cfg: Config,
    state: dict,
    target_ref: str = "",
    target_files: list[str] | None = None,
    target_description: str = "",
) -> None:
    """Shared agent-run path for NL/file/snippet inputs.

    Runs the agent against `repo_path`, optionally scoped to `target_files`,
    streams output, folds token usage back into the session, and prints the
    fresh-task separator. All run paths funnel through here so accounting and
    error handling stay identical.
    """
    run_cfg = cfg.model_copy(
        update={"agent": cfg.agent.model_copy(update={"max_tool_calls": state["budget"]})}
    )

    from ..agent import run_agent_sync

    renderer = StreamRenderer(_console)
    try:
        report = run_agent_sync(
            mode=mode,
            repo_path=str(repo_path),
            target_ref=target_ref,
            target_files=target_files or [],
            target_description=target_description,
            profile_name=state["profile"],
            config=run_cfg,
            on_event=renderer.handle,
        )
        # Carry forward per-session token totals so /cost is accurate
        state["session_tokens_in"] = state.get("session_tokens_in", 0) + report.total_input_tokens
        state["session_tokens_out"] = state.get("session_tokens_out", 0) + report.total_output_tokens
        state["session_cost_usd"] = state.get("session_cost_usd", 0.0) + report.est_cost_usd
        state["session_llm_calls"] = state.get("session_llm_calls", 0) + report.llm_call_count
    except KeyboardInterrupt:
        _console.print("\n[yellow]Investigation interrupted.[/]")
    except Exception as e:
        _console.print(f"\n  [red]✗[/] Agent failed: {e}")
        if os.environ.get("REVIO_DEBUG"):
            import traceback

            traceback.print_exc()
    finally:
        # Visual separator + owl mascot so the next prompt feels like
        # a fresh task, not a continuation. Skipped on non-TTY (CI).
        from .mascot import play_startup_animation

        _console.print()
        play_startup_animation(_console)
        _console.print()


# --- Pasted-code-snippet review ----------------------------------------------

# Fenced code block: ```lang\n...code...\n```  (lang tag optional)
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)

# Strong structural tokens that mark a line as "code, not prose".
_CODE_TOKEN_RE = re.compile(
    r"(;\s*$|[{}]\s*$|=>|\bdef\s|\bfunction\s|\bclass\s|\breturn\s|#include|"
    r"\bimport\s|\bpublic\s|\bprivate\s|\bstatic\s|\bconst\s|\blet\s|\bvar\s|"
    r"\bif\s*\(|\bfor\s*\(|\bwhile\s*\()"
)

# Language tag (from the fence) → file extension.
_LANG_EXT = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js", "jsx": ".jsx",
    "typescript": ".ts", "ts": ".ts", "tsx": ".tsx",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp", "cc": ".cpp", "h": ".h",
    "go": ".go", "golang": ".go", "rust": ".rs", "rs": ".rs",
    "java": ".java", "kotlin": ".kt", "kt": ".kt",
    "ruby": ".rb", "rb": ".rb", "php": ".php",
    "shell": ".sh", "bash": ".sh", "sh": ".sh",
    "lua": ".lua", "sql": ".sql",
    "verilog": ".v", "systemverilog": ".sv", "sv": ".sv",
    "st": ".st", "plc": ".st", "iecst": ".st",
}

# Active profile → default extension when no fence language tag is present.
_PROFILE_EXT = {"js": ".js", "python": ".py", "plc": ".st"}


def _extract_snippet(text: str) -> tuple[str, str | None, str] | None:
    """Detect a pasted code snippet in REPL input.

    Returns (code, lang_tag_or_None, surrounding_instruction) if `text` is a
    code snippet to review, else None.

    Two triggers (per the chosen UX): an explicit ```fenced``` block always
    wins; otherwise a CONSERVATIVE multi-line auto-detect catches obvious code
    (≥3 lines with strong structural tokens) without swallowing prose.
    """
    # 1) Explicit fenced block — most reliable. Text outside the fence is the
    #    user's instruction/focus.
    m = _FENCE_RE.search(text)
    if m:
        lang = (m.group(1) or "").strip().lower() or None
        code = m.group(2).strip("\n")
        instruction = (text[: m.start()] + " " + text[m.end():]).strip().strip("`").strip()
        if code.strip():
            return code, lang, instruction

    # 2) Conservative fence-less auto-detect: only when it really looks like a
    #    code paste, never for ordinary multi-line questions.
    lines = text.splitlines()
    nonblank = [ln for ln in lines if ln.strip()]
    if len(nonblank) < 3:
        return None
    code_like = sum(1 for ln in nonblank if _CODE_TOKEN_RE.search(ln))
    indented = sum(1 for ln in lines if ln[:1] in (" ", "\t") and ln.strip())
    # Require multiple structural-token lines AND that code dominates the input.
    if code_like >= 2 and (code_like + indented) >= len(nonblank) * 0.5:
        return text.strip("\n"), None, ""
    return None


def _ext_for_snippet(lang: str | None, state: dict) -> str:
    """Pick a file extension for a pasted snippet: fence tag → profile → .txt."""
    if lang and lang in _LANG_EXT:
        return _LANG_EXT[lang]
    return _PROFILE_EXT.get(state.get("profile", ""), ".txt")


def _handle_snippet_input(
    code: str, lang: str | None, instruction: str, cfg: Config, state: dict
) -> None:
    """Review a code snippet pasted straight into the chat box.

    The snippet is written to a throwaway temp dir as a single file, scanned
    with the same scoped pipeline as a single-file review, then cleaned up.
    """
    import shutil
    import tempfile

    ext = _ext_for_snippet(lang, state)
    tmpdir = Path(tempfile.mkdtemp(prefix="revio_snippet_"))
    try:
        fname = f"snippet{ext}"
        (tmpdir / fname).write_text(code, encoding="utf-8")

        desc = "the pasted code snippet"
        if instruction:
            desc += f' (user note: "{instruction[:200]}")'

        n_lines = len(code.splitlines())
        _console.print(
            f"  [dim]· reviewing pasted snippet[/] "
            f"[cyan]{fname}[/] [dim]({n_lines} lines)[/]"
        )
        _run_agent_on(
            mode=state["mode"],
            repo_path=tmpdir,
            cfg=cfg,
            state=state,
            target_files=[fname],
            target_description=desc,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _handle_nl_input(line: str, cfg: Config, state: dict) -> None:
    """Classify intent → run agent, change a setting, or explain the boundary."""
    classification = _classify_intent(line, cfg)
    if classification is None:
        _console.print("  [yellow]Could not classify request. Try a slash command or /help.[/]")
        return

    intent = classification.get("intent", "review")

    if intent == "config":
        _handle_config_intent(classification, cfg, state)
        return
    if intent in ("capability", "chat"):
        _print_capabilities(classification.get("rationale", ""))
        return
    if intent == "out_of_scope":
        _print_out_of_scope(classification.get("rationale", ""))
        return
    if intent not in ("review", "audit", "dedup"):
        # Unknown / unexpected label — fail safe by explaining the boundary
        # rather than silently launching an agent run.
        _print_out_of_scope(classification.get("rationale", ""))
        return

    # Resolve target path
    target_path = classification.get("target_path")
    if target_path:
        resolved = Path(target_path).expanduser()
        if not resolved.is_absolute():
            resolved = state["cwd"] / resolved
        resolved = resolved.resolve()
    else:
        resolved = state["cwd"]

    target_ref = classification.get("target_ref") or ""

    # Single-file scope: if the path points at a FILE (not a directory), root
    # the run at its git/parent dir and lock the review to just that one file.
    target_files: list[str] | None = None
    target_description = ""
    if resolved.is_file():
        file_path = resolved
        repo_root = _git_root_of(file_path) or file_path.parent
        try:
            rel = file_path.relative_to(repo_root).as_posix()
        except ValueError:
            repo_root = file_path.parent
            rel = file_path.name
        target_files = [rel]
        target_description = f"the single file `{rel}`"
        resolved = repo_root
        target_ref = ""  # no diff for a standalone file
        _console.print(f"  [dim]· scoping to single file:[/] [cyan]{rel}[/]")
    elif not resolved.is_dir():
        _console.print(f"  [red]✗[/] path not found: {resolved}")
        return

    _run_agent_on(
        mode=intent,
        repo_path=resolved,
        cfg=cfg,
        state=state,
        target_ref=target_ref,
        target_files=target_files,
        target_description=target_description,
    )


def _classify_intent(user_input: str, cfg: Config) -> dict | None:
    """Classify a multilingual NL request into a structured intent.

    Strategy (in order):
      1. Ask the LLM for strict JSON.
      2. If response is parseable JSON, use it.
      3. If not, fall back to a keyword classifier (no LLM needed).
         Better to give the user a *reasonable* guess than a flat error.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..agent.llm import APIKeyMissingError, make_llm

    # --- Try the LLM path first ---
    llm_text = ""
    try:
        llm = make_llm(cfg, max_tokens=400)
        resp = llm.invoke([SystemMessage(content=_INTENT_SYSTEM), HumanMessage(content=user_input)])
        llm_text = resp.content if isinstance(resp.content, str) else "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in resp.content
        )
    except APIKeyMissingError:
        _console.print("  [red]✗[/] API key not configured. Run /key or /config.")
        return None
    except Exception as e:
        _console.print(f"  [yellow]·[/] LLM classifier unreachable ({type(e).__name__}); using keyword fallback")
        return _keyword_classify(user_input)

    # --- Parse JSON (lenient: strip markdown fences, find longest brace block) ---
    parsed = _try_parse_json(llm_text)
    if parsed and isinstance(parsed, dict) and parsed.get("intent") in _VALID_INTENTS:
        return parsed

    # LLM returned prose / wrong shape — show it once for debugging, then fall back
    if os.environ.get("REVIO_DEBUG"):
        _console.print(f"  [dim]LLM intent response (not JSON):[/] [dim]{llm_text[:200]}[/]")
    _console.print("  [dim]·[/] LLM didn't return clean JSON; using keyword fallback")
    return _keyword_classify(user_input)


def _try_parse_json(text: str) -> dict | None:
    """Lenient JSON extraction from a possibly-prosy LLM response."""
    if not text:
        return None
    # Strip ```json ... ``` or ``` ... ``` fences
    fence = re.search(r"```(?:json)?\s*\n?(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    # Find the LONGEST top-level brace block
    candidates = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    candidates.sort(key=len, reverse=True)
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


# --- Keyword-based fallback classifier (no LLM, multilingual) ---------------

# Patterns are CASE-INSENSITIVE; one hit = winner. Order matters (specific
# intents first, generic last). Keep this short and battle-tested rather
# than try to cover every phrasing — the LLM path is the primary classifier.
_KEYWORD_RULES = [
    # dedup — finding duplication / dead code / AI-generated redundancy
    ("dedup", [
        "dedup", "redundan", "duplicate", "duplication", "dead code",
        "重复", "冗余", "去重", "死代码",
    ]),
    # audit — full-repo / security / vulnerabilities (NO specific commit ref)
    ("audit", [
        "audit", "scan", "vulnerab", "security", "exploit", "cve",
        "审计", "扫描", "漏洞", "安全", "全仓",
    ]),
    # review — diff / commit / PR / specific change
    ("review", [
        "review", "diff", "commit", "pull request", "this change",
        "review this file", "check this file", "look at this file",
        "审查", "检查这个", "看看这个", "看一下", "读取这个",
    ]),
]

# Path detection — Windows abs, UNC, Unix abs, relative, quoted.
_PATH_RE = re.compile(
    r"""(?xi)
    (?:
      "([^"]+\.[a-z0-9]{1,8})"                              # quoted file
    | '([^']+\.[a-z0-9]{1,8})'                              # quoted file (single)
    | ([A-Z]:\\[^\s"',]+)                                   # Windows absolute
    | (\\\\[^\s"',]+)                                       # UNC
    | (/(?:[^/\s"',]+/)*[^/\s"',]+\.[a-z0-9]{1,8})          # Unix absolute file
    | (\.{1,2}/[^\s"',]+)                                   # relative ./foo or ../foo
    )
    """
)

_GIT_REF_RE = re.compile(r"\b(HEAD(?:~\d+|\^+)?|[a-f0-9]{7,40})\b")


# Action verbs that signal "change a setting" (so a bare noun like "model"
# inside a review request doesn't get mis-routed to config). Multilingual.
_CONFIG_VERBS = (
    "set", "change", "switch", "update", "use", "configure", "make it",
    "show", "open",
    "改", "换", "设置", "设成", "调", "切换", "更新", "配置成", "用",
    "显示", "查看", "打开",
)


def _keyword_config_slash(text: str) -> str | None:
    """Map a config-ish NL request to a whitelisted slash command (or None).

    LLM is the primary router; this is the offline fallback. It is deliberately
    conservative — bare-noun cases (model / profile / mode / config) require an
    explicit action verb so review requests aren't swallowed.
    """
    low = text.lower()
    has_verb = any(v in low for v in _CONFIG_VERBS)

    # API key — strong signal, no verb needed. Never carries the value.
    if any(k in low for k in ("api key", "apikey", "api-key", "密钥", "钥匙")):
        return "/key"

    # Endpoint / URL — a literal URL or "endpoint" wording.
    if any(k in low for k in ("endpoint", "api url", "base url", "接口", "端点")) or (
        "url" in low and has_verb
    ):
        m = re.search(r"https?://[^\s'\"]+", text)
        return f"/url {m.group(0)}" if m else "/url"

    # Budget — "budget"/"预算" plus a number.
    if any(k in low for k in ("budget", "预算", "额度")):
        m = re.search(r"\b(\d{1,3})\b", text)
        return f"/budget {m.group(1)}" if m else "/budget"

    # Session cost.
    if any(k in low for k in ("cost", "成本", "花费", "费用", "花了多少", "多少钱")):
        return "/cost"

    # Model — a model-id-looking token, or an explicit list request.
    # (Checked BEFORE "mode" because the word "model" contains "mode".)
    if any(k in low for k in ("model", "模型")):
        if any(k in low for k in ("list", "available", "有哪些", "列出", "支持哪些")):
            return "/model list"
        if has_verb:
            m = re.search(r"\b([a-z][a-z0-9.]*-[a-z0-9.\-]+)\b", low)
            if m:
                return f"/model {m.group(1)}"
            return "/model"

    # Mode (word-boundary so the word "model" can't trigger it).
    if (re.search(r"\bmode\b", low) or "模式" in low) and has_verb:
        for mo in ("review", "audit", "dedup"):
            if mo in low:
                return f"/mode {mo}"
        return "/mode"

    # Profile.
    if any(k in low for k in ("profile", "配置文件")) and has_verb:
        for pr in ("auto", "js", "plc", "python"):
            if pr in low:
                return f"/profile {pr}"
        return "/profile"

    # Show/open the config file.
    if any(k in low for k in ("config", "settings", "配置", "设置")) and has_verb:
        return "/config"

    return None


def _keyword_classify(user_input: str) -> dict:
    """Best-effort classification when the LLM path fails.

    Always returns a usable dict — never None. Path extraction is regex-based
    and works on Windows / UNC / Unix / relative paths and quoted strings.
    """
    base = {
        "intent": "capability",
        "slash": None,
        "target_path": None,
        "target_ref": None,
        "focus_area": None,
        "rationale": "keyword fallback (LLM classifier unavailable or invalid)",
    }

    # Settings change first — most specific.
    slash = _keyword_config_slash(user_input)
    if slash:
        return {**base, "intent": "config", "slash": slash,
                "rationale": "keyword fallback (settings change)"}

    lower = user_input.lower()
    intent = None
    for kind, keywords in _KEYWORD_RULES:
        if any(k in lower for k in keywords):
            intent = kind
            break

    # Extract first path-shaped token
    target_path = None
    for groups in _PATH_RE.findall(user_input):
        for g in groups:
            if g:
                target_path = g
                break
        if target_path:
            break

    if intent is None:
        # A path with no review verb is most likely "look at this file";
        # otherwise we can't tell offline — show capabilities rather than
        # guessing or claiming it's out of scope.
        intent = "review" if target_path else "capability"

    target_ref_m = _GIT_REF_RE.search(user_input)
    target_ref = target_ref_m.group(1) if target_ref_m else None

    return {**base, "intent": intent, "target_path": target_path,
            "target_ref": target_ref}
