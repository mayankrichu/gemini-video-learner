from __future__ import annotations

from decimal import Decimal


def calculate_provider_cost_usd(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    input_usd_per_million: float,
    cached_input_usd_per_million: float,
    output_usd_per_million: float,
) -> Decimal:
    """Calculate model token cost from actual usage and a stored pricing snapshot."""
    cached_tokens = min(max(0, cached_input_tokens), max(0, input_tokens))
    uncached_tokens = max(0, input_tokens - cached_tokens)
    return (
        Decimal(uncached_tokens) * Decimal(str(input_usd_per_million)) / Decimal("1000000")
        + Decimal(cached_tokens) * Decimal(str(cached_input_usd_per_million)) / Decimal("1000000")
        + Decimal(max(0, output_tokens)) * Decimal(str(output_usd_per_million)) / Decimal("1000000")
    )
