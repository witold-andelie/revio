"""Token cost estimation across LLM providers.

Pricing is in **USD per 1 million tokens** — `(input_rate, output_rate)`.
Updated 2026-05. Matching is fuzzy so model variants ("claude-3-5-sonnet-20241022",
"deepseek-chat") fold to the right family without per-version maintenance.

Returns 0.0 for unknown models — never block the UX on a missing entry.
"""

from __future__ import annotations


# (input USD/1M, output USD/1M)
PRICING: dict[str, tuple[float, float]] = {
    # DeepSeek (OpenAI-compat) — primary because it's our default provider
    "deepseek-v4-flash":       (0.14, 0.55),    # current cheap tier
    "deepseek-v4-pro":         (0.27, 1.10),    # current flagship
    "deepseek-chat":           (0.27, 1.10),    # legacy alias
    "deepseek-reasoner":       (0.55, 2.19),    # legacy reasoner
    "deepseek":                (0.27, 1.10),    # fallback for any deepseek-*
    # Anthropic
    "claude-opus-4-7":         (15.0, 75.0),
    "claude-opus":             (15.0, 75.0),
    "claude-sonnet-4-6":       (3.0,  15.0),
    "claude-sonnet":           (3.0,  15.0),
    "claude-haiku-4-5":        (1.0,  5.0),
    "claude-haiku":            (1.0,  5.0),
    # OpenAI
    "gpt-4o":                  (2.5,  10.0),
    "gpt-4o-mini":             (0.15, 0.60),
    "gpt-4.1":                 (2.5,  10.0),
    "o1":                      (15.0, 60.0),
    # Mistral / Mixtral — EU-sovereign open-weight family
    "mistral-large":           (2.0,  6.0),          # frontier (123B)
    "mistral-small":           (0.2,  0.6),          # 22B
    "mistral-medium":          (0.4,  2.0),
    "codestral":               (0.2,  0.6),          # code-specialized 22B
    "ministral":               (0.04, 0.04),         # edge (3B / 8B)
    "mixtral":                 (0.7,  0.7),          # 8x7B / 8x22B MoE
    "mistral":                 (0.2,  0.6),          # generic fallback for mistral-*
    # Local (Ollama / llama.cpp) — assume free
    "llama":                   (0.0,  0.0),
    "qwen":                    (0.0,  0.0),
    "ollama":                  (0.0,  0.0),
}


def _resolve_pricing(model: str) -> tuple[float, float]:
    """Fuzzy-match a model string to a pricing tuple. Returns (0, 0) if unknown."""
    if not model:
        return (0.0, 0.0)
    m = model.lower()
    # Prefer the longest matching key (avoid 'claude' matching when
    # 'claude-opus-4-7' is the right entry).
    best = ""
    for key in PRICING:
        if key in m and len(key) > len(best):
            best = key
    return PRICING[best] if best else (0.0, 0.0)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for one LLM call. Unknown model → 0.0."""
    in_rate, out_rate = _resolve_pricing(model)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def is_priced(model: str) -> bool:
    """True if we have pricing data for this model — used by the renderer
    to decide whether to show a cost figure at all. For unknown models we
    suppress the $ amount entirely rather than misleading the user with $0.00."""
    return _resolve_pricing(model) != (0.0, 0.0)


def format_cost(usd: float) -> str:
    """Pretty-print a USD cost figure with appropriate precision."""
    if usd <= 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


def format_count(n: int) -> str:
    """Human-friendly token count: 1234 → '1.2k', 1500000 → '1.50M'."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}k"
    return f"{n / 1_000_000:.2f}M"
