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
import sys
from pathlib import Path
from typing import Callable, Optional

import questionary
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
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
    "/url":      "Change API endpoint URL (usage: /url <url>)",
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


_INTENT_SYSTEM = """You are an intent classifier for the revio code-review CLI.

The user typed a free-form request. Classify it into one of:

- review : the user wants to review a diff or specific commit
- audit  : the user wants a full-repo security audit (no diff context)
- dedup  : the user wants to find AI-generated redundancy / duplicate code
- chat   : the user is asking a meta-question (not a review task)

The user may type in ANY human language: English, Chinese (中文),
German (Deutsch), French (français), Spanish (español), Czech (česky),
Japanese (日本語), or any other. Classify the intent regardless of language.

Also extract optional structured fields:
- target_path : relative or absolute filesystem path mentioned (string or null)
- target_ref  : a git commit, branch, or "HEAD" if mentioned (string or null)
- focus_area  : security / performance / readability / etc. (string or null)

Respond with strict JSON in this shape:
{
  "intent": "review" | "audit" | "dedup" | "chat",
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
        self._slash = WordCompleter(list(SLASH_COMMANDS.keys()), ignore_case=True)
        self._path = PathCompleter(expanduser=True)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            yield from self._slash.get_completions(document, complete_event)
        else:
            # Best-effort path completion when looking at a path-shaped token
            last = text.split()[-1] if text.split() else ""
            if last.startswith("/") or last.startswith("~") or last.startswith("."):
                yield from self._path.get_completions(document, complete_event)


# --- REPL session -------------------------------------------------------------


def _history_path() -> Path:
    p = Path.home() / ".cache" / "revio" / "repl_history"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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

    session = PromptSession(
        history=FileHistory(str(_history_path())),
        auto_suggest=AutoSuggestFromHistory(),
        completer=_ReplCompleter(),
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

        if line.startswith("/"):
            keep_going = _handle_slash(line, cfg, state)
            if not keep_going:
                return
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
            "  • [dim]find duplicate functions in src/[/]\n"
            "  • [dim]/model claude-opus-4-5[/]",
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
        "request and classified into review / audit / dedup. Multilingual OK.\n"
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


def _cmd_url(arg: str, cfg: Config, state: dict) -> bool:
    if not arg:
        _console.print(f"  current url: [cyan]{cfg.llm.api_url}[/]")
        return True
    if not arg.startswith(("http://", "https://")):
        _console.print("  [red]✗[/] URL must start with http:// or https://")
        return True
    cfg.llm.api_url = arg
    save_user_config(cfg)
    _console.print(f"  [green]✓[/] api_url → [cyan]{arg}[/]")
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
    os.system(f'{editor} "{path}"')
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


def _handle_nl_input(line: str, cfg: Config, state: dict) -> None:
    """Classify intent → run agent in chosen mode."""
    classification = _classify_intent(line, cfg)
    if classification is None:
        _console.print("  [yellow]Could not classify request. Try a slash command or /help.[/]")
        return

    intent = classification.get("intent", "review")
    if intent == "chat":
        _console.print(
            f"  [dim]revio is task-focused; for general chat use claude directly.\n"
            f"  rationale: {classification.get('rationale', '')}[/]"
        )
        return

    # Resolve target path
    target_path = classification.get("target_path")
    if target_path:
        repo_path = Path(target_path).expanduser()
        if not repo_path.is_absolute():
            repo_path = state["cwd"] / repo_path
        repo_path = repo_path.resolve()
    else:
        repo_path = state["cwd"]

    target_ref = classification.get("target_ref") or ""

    # Override session budget into config
    run_cfg = cfg.model_copy(
        update={"agent": cfg.agent.model_copy(update={"max_tool_calls": state["budget"]})}
    )

    from ..agent import run_agent_sync

    renderer = StreamRenderer(_console)
    try:
        report = run_agent_sync(
            mode=intent,
            repo_path=str(repo_path),
            target_ref=target_ref,
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
    if parsed and isinstance(parsed, dict) and parsed.get("intent") in {"review", "audit", "dedup", "chat"}:
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


def _keyword_classify(user_input: str) -> dict:
    """Best-effort classification when the LLM path fails.

    Always returns a usable dict — never None. Worst case: intent='chat'
    so the user can re-phrase. Path extraction is regex-based and works
    on Windows / UNC / Unix / relative paths and quoted strings.
    """
    lower = user_input.lower()

    intent = "chat"
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

    # If a path was found but intent is still 'chat', bump to 'review'
    # (the most common "look at this file" pattern)
    if target_path and intent == "chat":
        intent = "review"

    target_ref_m = _GIT_REF_RE.search(user_input)
    target_ref = target_ref_m.group(1) if target_ref_m else None

    return {
        "intent": intent,
        "target_path": target_path,
        "target_ref": target_ref,
        "focus_area": None,
        "rationale": "keyword fallback (LLM classifier unavailable or invalid)",
    }
