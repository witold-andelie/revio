"""First-run setup wizard.

Runs interactively when no config file is found. Walks the user through:
  1. Which LLM provider
  2. API URL (per-provider default)
  3. API key (masked)
  4. Default model (per-provider suggestions)
  5. Disable thinking? (required for Mimo/reasoning models)
  6. Default profile (auto-detect on / off)
  7. Live connection test (optional, can skip)

Writes ~/.config/revio/config.toml with 0600 perms.
All wizard text is in English (per project convention).
"""

from __future__ import annotations

import sys
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel

from ..config import (
    AgentConfig,
    Config,
    FixConfig,
    LLMConfig,
    OutputConfig,
    ProfileConfig,
    save_user_config,
    user_config_path,
)


_console = Console()


# --- Provider presets ---------------------------------------------------------


_PROVIDER_DEFAULTS = {
    "Anthropic (official)": {
        "key": "anthropic",
        "url": "https://api.anthropic.com",
        "models": [
            "claude-sonnet-4-5",
            "claude-opus-4-5",
            "claude-haiku-4-5",
        ],
        "disable_thinking_default": False,
    },
    "Mimo / Xiaomi token plan": {
        "key": "mimo",
        "url": "https://token-plan-ams.xiaomimimo.com/anthropic",
        "models": [
            "mimo-v2.5-pro",
        ],
        "disable_thinking_default": True,
    },
    "DeepSeek": {
        "key": "openai_compat",
        "url": "https://api.deepseek.com",
        # Order matters — the wizard picks the first as the default suggestion.
        # Kept aligned with models_catalog.py (v4-pro = strongest;
        # deepseek-chat / deepseek-reasoner remain as legacy aliases that
        # still resolve on the deepseek endpoint).
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        "disable_thinking_default": False,
    },
    "OpenAI-compatible (OpenRouter / Together / ...)": {
        "key": "openai_compat",
        "url": "https://openrouter.ai/api/v1",
        "models": [],
        "disable_thinking_default": True,
    },
    "Custom endpoint": {
        "key": "custom",
        "url": "",
        "models": [],
        "disable_thinking_default": False,
    },
}


# --- Wizard entry point -------------------------------------------------------


def run_wizard() -> Config | None:
    """Run the interactive setup. Returns the saved Config or None on cancel."""

    _console.print()
    _console.print(
        Panel(
            "[bold]Welcome to revio[/]\n\n"
            "An agentic code-review CLI. Three modes:\n"
            "  • [cyan]review[/]  — investigate a diff or commit\n"
            "  • [cyan]audit[/]   — full-repo security audit\n"
            "  • [cyan]dedup[/]   — find and (optionally) fix AI-generated redundancy\n\n"
            "Let's set up your config (one-time).",
            border_style="cyan",
            title="🚀 First-run setup",
            title_align="left",
            padding=(1, 2),
        )
    )
    _console.print()

    # --- Step 1: provider ---
    provider_label = questionary.select(
        "LLM provider:",
        choices=list(_PROVIDER_DEFAULTS.keys()),
        default="Anthropic (official)",
    ).ask()
    if provider_label is None:
        return _cancelled()
    preset = _PROVIDER_DEFAULTS[provider_label]

    # --- Step 2: API URL ---
    default_url = preset["url"] or "https://api.anthropic.com"
    api_url = questionary.text(
        "API URL:",
        default=default_url,
        validate=lambda x: True if x.startswith(("http://", "https://")) else "URL must start with http:// or https://",
    ).ask()
    if api_url is None:
        return _cancelled()

    # --- Step 3: API key (masked) ---
    api_key = questionary.password(
        "API key (will be stored locally with 0600 perms):",
        validate=lambda x: True if x.strip() else "API key cannot be empty",
    ).ask()
    if api_key is None:
        return _cancelled()
    api_key = api_key.strip()

    # --- Step 4: model ---
    if preset["models"]:
        model_choices = preset["models"] + ["Other (enter manually)"]
        model_pick = questionary.select(
            "Default model:",
            choices=model_choices,
            default=preset["models"][0],
        ).ask()
        if model_pick is None:
            return _cancelled()
        if model_pick == "Other (enter manually)":
            model = questionary.text("Model ID:").ask()
            if not model:
                return _cancelled()
        else:
            model = model_pick
    else:
        model = questionary.text(
            "Model ID:",
            validate=lambda x: True if x.strip() else "Model ID cannot be empty",
        ).ask()
        if not model:
            return _cancelled()

    # --- Step 5: disable thinking ---
    disable_thinking = questionary.confirm(
        "Disable thinking mode? (Required for Mimo and some OpenAI-compatible providers)",
        default=preset["disable_thinking_default"],
    ).ask()
    if disable_thinking is None:
        return _cancelled()

    # --- Step 6: default profile ---
    profile_default = questionary.select(
        "Default profile when no --profile flag is given:",
        choices=[
            "auto  (detect from repo contents — recommended)",
            "js    (always JavaScript / TypeScript)",
            "plc   (always PLC / Structured Text)",
            "python (always Python)",
        ],
        default="auto  (detect from repo contents — recommended)",
    ).ask()
    if profile_default is None:
        return _cancelled()
    profile_name = profile_default.split()[0]

    # --- Step 7: connection test (optional) ---
    do_test = questionary.confirm(
        "Test connection now? (1 small API call)",
        default=True,
    ).ask()

    if do_test:
        ok, msg = _test_connection(preset["key"], api_url, api_key, model, disable_thinking)
        # Auto-fix a missing /v1 on OpenAI-compatible endpoints: model discovery
        # tolerates a bare host, but the chat path 404s without /v1. Normalize so
        # the saved config just works.
        if (
            not ok
            and preset["key"] in ("openai_compat", "custom")
            and not api_url.rstrip("/").endswith("/v1")
        ):
            alt = api_url.rstrip("/") + "/v1"
            ok2, msg2 = _test_connection(preset["key"], alt, api_key, model, disable_thinking)
            if ok2:
                api_url, ok, msg = alt, True, msg2
                _console.print(f"  [yellow]·[/] auto-added /v1 → [cyan]{alt}[/]")
        if ok:
            _console.print(f"  [green]✓[/] Connection OK — {msg}")
        else:
            _console.print(f"  [red]✗[/] {msg}")
            keep_going = questionary.confirm(
                "Save the config anyway?",
                default=False,
            ).ask()
            if not keep_going:
                return _cancelled()

    # --- Build and save ---
    cfg = Config(
        llm=LLMConfig(
            provider=preset["key"],
            api_url=api_url,
            api_key=api_key,
            model=model,
            disable_thinking=disable_thinking,
        ),
        agent=AgentConfig(),
        profile=ProfileConfig(default=profile_name),  # type: ignore[arg-type]
        output=OutputConfig(),
        fix=FixConfig(),
    )

    path = save_user_config(cfg)
    _console.print()
    _console.print(f"  [green]✓[/] Config saved to [cyan]{path}[/]")
    _console.print("  Run [bold]revio[/] anytime to start a session, or [bold]revio --help[/] for commands.")
    _console.print()

    return cfg


# --- Connection test ----------------------------------------------------------


def _test_connection(
    provider_key: str,
    api_url: str,
    api_key: str,
    model: str,
    disable_thinking: bool,
) -> tuple[bool, str]:
    """Make a tiny chat call to verify credentials work, for the right protocol."""
    from langchain_core.messages import HumanMessage

    try:
        if provider_key in ("anthropic", "mimo"):
            from langchain_anthropic import ChatAnthropic

            kwargs: dict = {
                "model": model, "api_key": api_key,
                "max_tokens": 16, "temperature": 0,
            }
            if api_url.rstrip("/") != "https://api.anthropic.com":
                kwargs["base_url"] = api_url
            if disable_thinking:
                kwargs["thinking"] = {"type": "disabled"}
            llm = ChatAnthropic(**kwargs)
        else:
            from langchain_openai import ChatOpenAI

            kwargs = {
                "model": model, "api_key": api_key,
                "max_tokens": 16, "temperature": 0,
                "base_url": api_url,
            }
            llm = ChatOpenAI(**kwargs)

        resp = llm.invoke([HumanMessage(content="reply with the single word 'ok'")])
        content = resp.content
        text = content if isinstance(content, str) else str(content)
        return True, f"got reply ({len(text)} chars)"
    except Exception as e:
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:200] + "…"
        return False, f"Connection failed: {msg}"


# --- Helpers ------------------------------------------------------------------


def _cancelled() -> None:
    _console.print()
    _console.print("  [yellow]Setup cancelled.[/] You can rerun anytime with [bold]revio config init[/].")
    return None


def needs_wizard() -> bool:
    """Return True if no config exists and we should run the wizard."""
    return not user_config_path().is_file()
