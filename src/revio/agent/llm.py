"""LLM factory — builds a chat-model instance from Config.

Supports two protocol families:

- **Anthropic protocol** (`provider = anthropic | mimo`)
   Native Anthropic Messages API + tool_use blocks. Uses ChatAnthropic.
   Custom base_url supported (Mimo, self-hosted Anthropic-compat gateways).

- **OpenAI-compatible protocol** (`provider = openai_compat | custom`)
   chat/completions endpoint + OpenAI tools spec. Uses ChatOpenAI.
   Targets: DeepSeek, OpenRouter, Together, vLLM, LM Studio, Ollama, etc.

For `custom`, we default to ChatOpenAI since most "custom" endpoints in
the wild are OpenAI-compatible. Users who want a custom Anthropic-compat
endpoint should set `provider = anthropic` + override `api_url`.
"""

from __future__ import annotations

from typing import Any

from ..config import Config


class APIKeyMissingError(RuntimeError):
    """Raised when no API key can be resolved."""


class UnsupportedProviderError(RuntimeError):
    """Raised for unknown provider values."""


# Providers that speak the Anthropic Messages API (incl. tool_use blocks).
_ANTHROPIC_PROVIDERS = {"anthropic", "mimo"}

# Providers that speak OpenAI's chat/completions + tools API.
_OPENAI_PROVIDERS = {"openai_compat", "custom"}

# Substrings in model IDs that flag the model as reasoner-style.
# These models reject `temperature` and emit `reasoning_content` that the
# server then rejects if echoed back in input messages.
_REASONER_MARKERS = ("reasoner", "-r1", "o1-", "o3-", "thinking", "qwq", "deepthink")


def _is_reasoner_model(model: str) -> bool:
    m = (model or "").lower()
    return any(tok in m for tok in _REASONER_MARKERS)


def make_llm(config: Config, max_tokens: int = 4096) -> Any:
    """Build a chat-model LLM from config, ready for tool binding.

    Returns either a ChatAnthropic or ChatOpenAI depending on provider.
    Both expose `.bind_tools(tools)` and `.ainvoke(messages)` with the
    same surface, so the rest of the agent code is protocol-agnostic.
    """
    api_key = config.llm.resolve_api_key()
    if not api_key:
        raise APIKeyMissingError(
            "No API key configured. Run `revio config init` or set the relevant env var."
        )

    provider = config.llm.provider

    if provider in _ANTHROPIC_PROVIDERS:
        return _make_anthropic(config, api_key, max_tokens)
    if provider in _OPENAI_PROVIDERS:
        return _make_openai(config, api_key, max_tokens)

    raise UnsupportedProviderError(
        f"unknown provider {provider!r} (expected one of: "
        f"{sorted(_ANTHROPIC_PROVIDERS | _OPENAI_PROVIDERS)})"
    )


# --- Anthropic ---------------------------------------------------------------


def _make_anthropic(config: Config, api_key: str, max_tokens: int):
    from langchain_anthropic import ChatAnthropic

    kwargs: dict = {
        "model": config.llm.model,
        "api_key": api_key,
        "max_tokens": max_tokens,
    }

    default_url = "https://api.anthropic.com"
    if config.llm.api_url and config.llm.api_url.rstrip("/") != default_url:
        kwargs["base_url"] = config.llm.api_url

    if config.llm.disable_thinking:
        # Thinking off — deterministic sampling is fine and is what the
        # rest of the agent assumes (grounding validator + JSON outputs).
        kwargs["thinking"] = {"type": "disabled"}
        kwargs["temperature"] = 0
    else:
        # Thinking on — Anthropic requires temperature=1 when extended
        # thinking is enabled; passing temperature=0 makes the API reject
        # the request. Budget of 2048 covers normal review/audit prompts
        # while staying well under max_tokens. Mimo and other Anthropic-
        # compat gateways that default-enable thinking also accept this.
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 2048}
        kwargs["temperature"] = 1

    return ChatAnthropic(**kwargs)


# --- OpenAI-compatible ------------------------------------------------------


def _make_openai(config: Config, api_key: str, max_tokens: int):
    from langchain_openai import ChatOpenAI

    kwargs: dict = {
        "model": config.llm.model,
        "api_key": api_key,
        "max_tokens": max_tokens,
    }

    if _is_reasoner_model(config.llm.model):
        # Reasoner models (deepseek-reasoner, o1, o3, qwq, glm-zero, ...)
        # reject `temperature` outright. Omit it; the API picks its own
        # internal sampling. Also note: react_node strips reasoning_content
        # from message history before each turn (see graph.py) because
        # these providers reject it as input.
        pass
    else:
        kwargs["temperature"] = 0

    if config.llm.api_url:
        # ChatOpenAI uses `base_url`; most OAI-compatible endpoints accept
        # the bare host or /v1 suffix interchangeably. We pass through as-is.
        kwargs["base_url"] = config.llm.api_url

    return ChatOpenAI(**kwargs)
