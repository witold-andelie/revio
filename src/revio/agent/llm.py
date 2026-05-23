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
        "temperature": 0,
    }

    default_url = "https://api.anthropic.com"
    if config.llm.api_url and config.llm.api_url.rstrip("/") != default_url:
        kwargs["base_url"] = config.llm.api_url

    if config.llm.disable_thinking:
        kwargs["thinking"] = {"type": "disabled"}

    return ChatAnthropic(**kwargs)


# --- OpenAI-compatible ------------------------------------------------------


def _make_openai(config: Config, api_key: str, max_tokens: int):
    from langchain_openai import ChatOpenAI

    kwargs: dict = {
        "model": config.llm.model,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    if config.llm.api_url:
        # ChatOpenAI uses `base_url`; most OAI-compatible endpoints accept
        # the bare host or /v1 suffix interchangeably. We pass through as-is.
        kwargs["base_url"] = config.llm.api_url

    return ChatOpenAI(**kwargs)
