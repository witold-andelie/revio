"""Model discovery + curated catalog per provider.

Goal: when the user says `/model`, they should see what models the
**current API endpoint** actually supports, not just be told "type a
string and hope". Two paths:

1. **Live discovery** — for OpenAI-compatible endpoints (DeepSeek,
   Together, Groq, Ollama, any vLLM) we hit `GET {api_url}/v1/models`
   which is the standard. Returns the actual catalog the server is
   willing to serve right now.

2. **Curated fallback** — for endpoints that don't expose /models, or
   when discovery fails, we ship a hand-maintained list of the popular
   models per known provider. The list is the smallest useful set, not
   exhaustive; users can still type any model string by hand.

This module never raises on network errors — the UX never blocks.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


@dataclass
class ModelEntry:
    name: str            # the exact ID the API expects
    note: str = ""       # human-readable hint ("128k ctx", "reasoning", ...)
    source: str = ""     # "discovered" | "curated"


# Curated per-host substring matches. When the api_url contains the key,
# we offer the corresponding model list as suggestions.
_CURATED: dict[str, list[ModelEntry]] = {
    "deepseek.com": [
        ModelEntry("deepseek-v4-pro",     "strongest, recommended"),
        ModelEntry("deepseek-v4-flash",   "cheaper + faster"),
        ModelEntry("deepseek-chat",       "legacy alias, may still work"),
        ModelEntry("deepseek-reasoner",   "legacy reasoner"),
    ],
    "api.anthropic.com": [
        ModelEntry("claude-opus-4-7",         "top-tier, expensive"),
        ModelEntry("claude-sonnet-4-6",       "balanced (revio default)"),
        ModelEntry("claude-haiku-4-5-20251001", "cheapest, fastest"),
    ],
    "api.openai.com": [
        ModelEntry("gpt-4o",      "general-purpose"),
        ModelEntry("gpt-4o-mini", "10× cheaper than gpt-4o"),
        ModelEntry("gpt-4.1",     "latest flagship"),
        ModelEntry("o1",          "reasoning model, expensive"),
    ],
    "freemodel.dev": [
        ModelEntry("claude-sonnet-4-5", "gateway model"),
        ModelEntry("claude-opus-4-5",   "gateway model"),
    ],
    "api.mistral.ai": [
        ModelEntry("mistral-large-latest", "flagship"),
        ModelEntry("mistral-small-latest", "cheap"),
    ],
    "localhost": [
        ModelEntry("llama3.1:8b",  "via Ollama, free"),
        ModelEntry("qwen2.5:7b",   "via Ollama, free"),
    ],
}


def curated_for(api_url: str) -> list[ModelEntry]:
    """Return the curated entries whose host substring matches api_url."""
    if not api_url:
        return []
    for key, entries in _CURATED.items():
        if key in api_url:
            return list(entries)
    return []


def discover_from_endpoint(
    api_url: str,
    api_key: str,
    *,
    timeout: float = 4.0,
) -> list[ModelEntry]:
    """GET {api_url}/v1/models (OpenAI-compat). Empty list on any failure.

    The /models endpoint is a de-facto standard across OpenAI-compatible
    providers (DeepSeek, Together, Groq, OpenRouter, vLLM, Ollama, etc.).
    Anthropic's native endpoint does NOT support it — for native Anthropic
    we rely on the curated list.
    """
    if not api_url:
        return []
    url = api_url.rstrip("/")
    if not url.endswith(("/v1", "/v1/")):
        url = url + "/v1/models"
    else:
        url = url + "/models"

    try:
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []

    out: list[ModelEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name")
        if not mid:
            continue
        out.append(ModelEntry(name=str(mid), source="discovered"))
    return out


def list_models_for(api_url: str, api_key: str) -> list[ModelEntry]:
    """Discovery-first, then fall back to curated. Deduplicates by name.

    Discovered entries win — they are authoritative for what the endpoint
    is willing to serve right now. Curated entries only fill in if
    discovery turned up nothing (offline, native-Anthropic, etc.).
    """
    discovered = discover_from_endpoint(api_url, api_key)
    if discovered:
        # Decorate with curated notes when names match
        notes = {e.name: e.note for e in curated_for(api_url)}
        for entry in discovered:
            if entry.name in notes:
                entry.note = notes[entry.name]
        return discovered

    return curated_for(api_url)


def format_table(entries: Iterable[ModelEntry], current: str = "") -> list[str]:
    """Render entries as aligned lines for printing."""
    entries = list(entries)
    if not entries:
        return ["  (no models found — try `/model <name>` to set manually)"]
    width = max(len(e.name) for e in entries)
    lines: list[str] = []
    for e in entries:
        marker = "[bold green]●[/]" if e.name == current else "[dim]·[/]"
        name = e.name.ljust(width)
        note = f"[dim]— {e.note}[/]" if e.note else ""
        src = "" if e.source != "discovered" else " [dim](live)[/]"
        lines.append(f"  {marker} [cyan]{name}[/]{src}  {note}")
    return lines
