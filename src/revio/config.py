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
    """Minimal TOML serializer (avoids adding a write-only dep)."""
    lines: list[str] = []
    # Top-level scalars first (none in our schema, but safe)
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for k, v in scalars.items():
        lines.append(f"{k} = {_toml_value(v)}")
    if scalars and tables:
        lines.append("")
    for tname, tdata in tables.items():
        lines.append(f"[{tname}]")
        for k, v in tdata.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
    return f'"{v}"'
