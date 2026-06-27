"""Configuration loading and persistence.

Layering (later overrides earlier):
1. Built-in defaults (this file)
2. ~/.config/revio/config.toml          (user global, written by first-run wizard)
3. ./.revio.toml                          (project override, can be committed)
4. ./.revio.local.toml                    (project override, NOT committed)
5. Environment variables (REVIO_*)

API keys are NEVER read from .revio.toml (gets committed). They live in:
- ~/.config/revio/config.toml (with 0600 perms), OR
- Environment variable referenced by `api_key_env`
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Literal

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# --- Config schema -------------------------------------------------------------


Provider = Literal["anthropic", "mimo", "openai_compat", "custom"]
ProfileName = Literal["auto", "js", "plc", "python"]
OutputFormat = Literal["stream", "json", "markdown"]
FixMode = Literal["ask", "dry-run", "yes"]


class LLMConfig(BaseModel):
    provider: Provider = "anthropic"
    api_url: str = "https://api.anthropic.com"
    api_key: str = ""            # Stored directly (user choice — see init wizard)
    api_key_env: str = ""        # OR: name of env var to read from
    model: str = "claude-sonnet-4-5"
    disable_thinking: bool = False
    # Opt-in: use OpenAI's Responses API (/v1/responses) instead of Chat
    # Completions for openai_compat/custom endpoints. Off by default — most
    # OpenAI-compatible providers only speak chat/completions. Only enable for
    # endpoints that support it (OpenAI, Azure, GPT-5.x / Codex proxies).
    use_responses_api: bool = False

    def resolve_api_key(self) -> str:
        """Get the API key from direct field or env var."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        # Last resort fallback
        return os.environ.get("ANTHROPIC_API_KEY", "")


class AgentConfig(BaseModel):
    max_tool_calls: int = Field(default=15, ge=1, le=200)
    confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    budget_tokens_per_run: int = Field(default=100_000, ge=1000)
    checkpoint_dir: str = "~/.cache/revio"


class ProfileConfig(BaseModel):
    default: ProfileName = "auto"
    auto_confirm: bool = False   # Whether auto-detect skips its confirmation prompt


class OutputConfig(BaseModel):
    default_format: OutputFormat = "stream"
    verbose_reasoning: bool = True


class FixConfig(BaseModel):
    default_mode: FixMode = "ask"
    min_confidence_for_yes: float = Field(default=0.95, ge=0.0, le=1.0)


class FixHistoryConfig(BaseModel):
    """Bounds for the auto-managed fix-undo history.

    Each `--fix` session snapshots affected files BEFORE applying so the
    user can `revio fix undo` later (no git required). These caps prevent
    the cache directory from growing unbounded.
    """

    max_sessions: int = Field(default=50, ge=1, le=10_000)
    max_age_days: int = Field(default=30, ge=1, le=3650)
    max_file_bytes: int = Field(default=1_048_576, ge=1024)  # 1 MiB


class MemoryConfig(BaseModel):
    """Caps for the auto-pruned on-disk memory stores.

    All cleanup is COUNT-based: when a store exceeds its cap, the OLDEST
    entries are deleted (no time/age-based expiry). Caps are deliberately
    generous so normal use effectively never trims, but the stores can never
    grow without bound.

    - findings_max_rows     : rows kept in each repo's findings_history table
    - checkpoint_max_runs   : past runs (threads) kept per repo checkpoint DB
    - repl_history_max_entries : commands kept in the REPL history file
    """

    findings_max_rows: int = Field(default=5000, ge=100, le=1_000_000)
    checkpoint_max_runs: int = Field(default=50, ge=2, le=100_000)
    repl_history_max_entries: int = Field(default=1000, ge=50, le=1_000_000)


class MCPServerSpec(BaseModel):
    """One MCP server entry in the user's config."""

    # stdio transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # sse/http transport
    url: str | None = None
    api_key_env: str | None = None
    # connection timeout
    timeout: float = Field(default=5.0, ge=0.5, le=60.0)
    # disable without removing the entry
    enabled: bool = True


class MCPConfig(BaseModel):
    """[mcp.servers.<name>] tables in config.toml."""

    servers: dict[str, MCPServerSpec] = Field(default_factory=dict)


class Config(BaseModel):
    """Aggregate config from all sources."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    fix: FixConfig = Field(default_factory=FixConfig)
    fix_history: FixHistoryConfig = Field(default_factory=FixHistoryConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)


# --- File paths ----------------------------------------------------------------


def user_config_dir() -> Path:
    """~/.config/revio (Linux/Mac) or %APPDATA%/revio (Windows)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "revio"


def user_config_path() -> Path:
    return user_config_dir() / "config.toml"


def project_config_paths(cwd: Path | None = None) -> list[Path]:
    """Project-level overrides, in load order (later wins)."""
    cwd = cwd or Path.cwd()
    return [cwd / ".revio.toml", cwd / ".revio.local.toml"]


# --- Loading -------------------------------------------------------------------


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive dict merge — overlay wins on conflicts."""
    out = dict(base)
    for key, val in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


class _EnvOverrides(BaseSettings):
    """Lightweight env-var reader for REVIO_* overrides."""

    model_config = SettingsConfigDict(env_prefix="REVIO_", env_nested_delimiter="__", extra="ignore")


def load_config(cwd: Path | None = None) -> Config:
    """Load merged config from all sources."""
    merged: dict = {}

    # User global
    merged = _deep_merge(merged, _load_toml(user_config_path()))

    # Project overrides
    for p in project_config_paths(cwd):
        merged = _deep_merge(merged, _load_toml(p))

    # TODO: Env var overrides (post-MVP)

    return Config.model_validate(merged) if merged else Config()


def config_exists() -> bool:
    """True if at least one config file exists (anywhere in the search chain)."""
    if user_config_path().is_file():
        return True
    for p in project_config_paths():
        if p.is_file():
            return True
    return False


# --- Writing -------------------------------------------------------------------


def save_user_config(cfg: Config) -> Path:
    """Write user-global config with 0600 perms (owner read/write only)."""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_to_toml(cfg.model_dump()), encoding="utf-8")
    # Restrict perms (Mac/Linux; Windows no-op-ish)
    if sys.platform != "win32":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _to_toml(data: dict) -> str:
    """Minimal TOML serializer for our Config schema (no write-only dep).

    Handles nested tables (`[mcp.servers.jira]`) recursively. Empty dicts
    are SKIPPED — they round-trip as Pydantic defaults, and emitting
    `mcp.servers = ""` (the bug we had) made the loader choke on next
    startup.
    """
    lines: list[str] = []
    _emit_section(data, prefix=[], lines=lines)
    return "\n".join(lines).rstrip() + "\n"


def _emit_section(data: dict, prefix: list[str], lines: list[str]) -> None:
    """Emit one TOML section + recurse for nested tables.

    `prefix` is the dotted path so far ([] = top-level, ['mcp'] = inside
    `[mcp]`, etc.). Scalars / lists are emitted first under the current
    header; then each nested dict gets its own `[section.subsection]`.
    """
    scalars = {}
    nested: dict[str, dict] = {}
    for k, v in data.items():
        if v is None:
            continue  # TOML has no null; skip and let Pydantic default fill in
        if isinstance(v, dict):
            if not v:
                continue  # skip empty tables — Pydantic defaults handle them
            nested[k] = v
        else:
            scalars[k] = v

    if scalars and prefix:
        # Emit the header for the current section (skipped at top-level)
        lines.append(f"[{'.'.join(prefix)}]")
    elif scalars and not prefix and lines:
        # Top-level scalars with prior content — keep them grouped
        pass

    for k, v in scalars.items():
        lines.append(f"{k} = {_toml_value(v)}")
    if scalars:
        lines.append("")

    for nname, ndata in nested.items():
        new_prefix = prefix + [nname]
        # Only emit the header HERE if the nested dict has scalar fields
        # of its own; otherwise the recursive call will emit deeper headers.
        if any(not isinstance(x, dict) for x in ndata.values()):
            lines.append(f"[{'.'.join(new_prefix)}]")
            sub_scalars = {k: v for k, v in ndata.items() if not isinstance(v, dict) and v is not None}
            sub_nested  = {k: v for k, v in ndata.items() if isinstance(v, dict) and v}
            for k, v in sub_scalars.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")
            for nn, nd in sub_nested.items():
                _emit_section({nn: nd}, prefix=new_prefix, lines=lines)
        else:
            # Pure-nested dict (e.g. mcp.servers = {jira: {...}}) — recurse
            _emit_section(ndata, prefix=new_prefix, lines=lines)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # Escape backslashes and quotes
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        # Inline-table form: { k = v, k2 = v2 } — but only callsite that
        # reaches here is when we deliberately want a one-line dict.
        # For our schema we route dicts through _emit_section instead;
        # this branch exists so list-of-dicts doesn't blow up.
        items = ", ".join(f"{k} = {_toml_value(val)}" for k, val in v.items())
        return "{" + items + "}"
    return f'"{v}"'
