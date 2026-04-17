"""Model pricing table (USD per million tokens).

Prices are intentionally maintained in this repo rather than fetched from a
vendor API; the prices a run was billed at are a persistent property of the
run, not of today. Users override with :func:`register_price` to keep
historical runs consistent when vendor prices change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """Per-million-token cost in USD."""

    input_per_million: float
    output_per_million: float


# Conservative defaults — users should override for billing-accurate
# numbers. Costs are rounded to 2 decimals of cents/Mtok.
_PRICES: dict[str, ModelPrice] = {
    # Anthropic (public list prices, approximate)
    "claude-opus-4-5": ModelPrice(15.00, 75.00),
    "claude-sonnet-4-5": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
    # OpenAI (public list prices, approximate)
    "gpt-4o": ModelPrice(2.50, 10.00),
    "gpt-4o-mini": ModelPrice(0.15, 0.60),
    # Fallback for unknown models: free, so cost accounting just zeros out.
    "mock": ModelPrice(0.0, 0.0),
}


def get_price(model: str) -> ModelPrice:
    return _PRICES.get(model, ModelPrice(0.0, 0.0))


def register_price(model: str, price: ModelPrice) -> None:
    _PRICES[model] = price


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = get_price(model)
    return (
        input_tokens * p.input_per_million / 1_000_000.0
        + output_tokens * p.output_per_million / 1_000_000.0
    )


__all__ = ["ModelPrice", "estimate_cost_usd", "get_price", "register_price"]
