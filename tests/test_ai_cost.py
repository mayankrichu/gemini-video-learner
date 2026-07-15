from __future__ import annotations

from decimal import Decimal

from app.pricing import calculate_provider_cost_usd


def test_provider_cost_uses_cached_and_uncached_rates() -> None:
    cost = calculate_provider_cost_usd(
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        output_tokens=500_000,
        input_usd_per_million=0.25,
        cached_input_usd_per_million=0.025,
        output_usd_per_million=2.0,
    )
    assert cost == Decimal("1.205")


def test_cached_tokens_are_clamped_to_total_input() -> None:
    cost = calculate_provider_cost_usd(
        input_tokens=100,
        cached_input_tokens=200,
        output_tokens=0,
        input_usd_per_million=1.0,
        cached_input_usd_per_million=0.5,
        output_usd_per_million=1.0,
    )
    assert cost == Decimal("0.00005")
