"""Cost estimation. Prices are best-effort and meant for a rough running total, not an invoice — this
table *will* go stale and is not guaranteed accurate even today. Publish your own numbers in
`scoutqa.yaml` (`llm.pricing`) for anything that matters, and check the provider's current price page.

$ per 1M tokens. `None` means "unknown provider/model — track tokens only, not cost".
"""

from __future__ import annotations

from dataclasses import dataclass

from scoutqa.llm.base import Usage

# (input, cached_input, output) — cached input is normally billed far below full input price.
# Anthropic/OpenAI model names below are confirmed current (present in each SDK's own model list at the
# time this was written); the Gemini names are illustrative — Google has no equivalent closed list to
# check against, so confirm the exact model string and its price before relying on this for real spend.
_PRICES_PER_M: dict[str, tuple[float, float, float]] = {
    "anthropic:claude-opus-5-5": (15.0, 1.5, 75.0),
    "anthropic:claude-sonnet-5": (3.0, 0.3, 15.0),
    "anthropic:claude-haiku-4-5-20251001": (1.0, 0.1, 5.0),
    "openai:gpt-5.1": (1.25, 0.125, 10.0),
    "openai:gpt-5.1-mini": (0.25, 0.025, 2.0),
    "openai:gpt-5-nano": (0.05, 0.005, 0.4),
    "gemini:gemini-3-pro": (2.0, 0.5, 12.0),
    "gemini:gemini-3-flash": (0.15, 0.0375, 0.6),
}


@dataclass(frozen=True)
class Price:
    input_per_m: float
    cached_input_per_m: float
    output_per_m: float


def price_for(provider: str, model: str,
             overrides: dict[str, tuple[float, float, float]] | None = None) -> Price | None:
    key = f"{provider}:{model}"
    table = {**_PRICES_PER_M, **(overrides or {})}
    found = table.get(key)
    if found is None and provider == "azure_openai":  # Azure deployment names are account-specific
        found = table.get(f"openai:{model}")
    return Price(*found) if found else None


def estimate_cost_usd(usage: Usage, price: Price | None) -> float | None:
    if price is None:
        return None
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    return (uncached * price.input_per_m + usage.cached_input_tokens * price.cached_input_per_m
            + usage.output_tokens * price.output_per_m) / 1_000_000
