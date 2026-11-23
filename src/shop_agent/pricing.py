"""Estimated cost of a run, from token usage.

Prices are USD per million tokens, first-party Claude API list prices as of
November 2026. This is an estimate for monitoring, not a bill: check the
console for real spend, and update the table when prices change.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        # Writing to the 5-minute prompt cache costs 1.25x the input price.
        return self.input * 1.25


PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(input=8.00, output=40.00, cache_read=0.20),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01),
}


@dataclass
class Usage:
    """Token counts, summed over every model call in a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage) -> None:
        """Add the usage block of one API response."""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        # The cache fields are None when caching did not apply.
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens or 0
        self.cache_read_input_tokens += usage.cache_read_input_tokens or 0


def estimate_cost(model: str, usage: Usage) -> float | None:
    """Estimated USD cost, or None for a model missing from the table."""
    price = PRICES.get(model)
    if price is None:
        return None
    # input_tokens excludes cached tokens, which are billed at their own rates.
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_creation_input_tokens * price.cache_write
        + usage.cache_read_input_tokens * price.cache_read
    )
    return round(total / 1_000_000, 6)
