"""Model pricing and cost calculation."""

from __future__ import annotations

# Prices per token (USD)
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5-20250514": {"input": 3.0 / 1e6, "output": 15.0 / 1e6},
    "claude-sonnet-4-5-20250929": {"input": 3.0 / 1e6, "output": 15.0 / 1e6},
    "claude-sonnet-4-6": {"input": 3.0 / 1e6, "output": 15.0 / 1e6},
    "claude-opus-4-6": {"input": 15.0 / 1e6, "output": 75.0 / 1e6},
    "claude-haiku-4-5-20251001": {"input": 0.80 / 1e6, "output": 4.0 / 1e6},
}

# Fallback for unknown models
_DEFAULT_PRICING = {"input": 3.0 / 1e6, "output": 15.0 / 1e6}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for a given model and token counts."""
    rates = PRICING.get(model, _DEFAULT_PRICING)
    return input_tokens * rates["input"] + output_tokens * rates["output"]
