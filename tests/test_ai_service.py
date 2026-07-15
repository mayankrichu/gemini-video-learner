from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.ai_service import (
    AITranslationError,
    AITranslationResult,
    AITranslationService,
    OpenAIUsage,
)
from app.models import AIOutputItem
from app.vocab import AICandidate


def make_candidate(index: int) -> AICandidate:
    return AICandidate(
        id=f"v{index:05d}",
        type="verb",
        lemma=f"verb{index}",
        contexts=[f"Kontext {index}"],
    )


def make_output(candidate: AICandidate) -> AIOutputItem:
    return AIOutputItem(
        id=candidate.id,
        type="verb",
        word=candidate.lemma,
        translation=f"translation {candidate.id}",
        example=f"Wir benutzen {candidate.lemma}.",
        example_translation=f"We use {candidate.lemma}.",
        level="B1",
    )


@pytest.mark.asyncio
async def test_translate_preserves_batch_order_with_concurrency() -> None:
    service = object.__new__(AITranslationService)
    service.settings = SimpleNamespace(  # type: ignore[assignment]
        openai_batch_size=2,
        openai_concurrency=2,
    )

    async def fake_translate_batch(batch: list[AICandidate]) -> AITranslationResult:
        # Complete the second batch first to prove response ordering is deterministic.
        await asyncio.sleep(0.02 if batch[0].id == "v00001" else 0)
        return AITranslationResult(
            items=[make_output(candidate) for candidate in batch],
            usage=OpenAIUsage(
                input_tokens=len(batch),
                output_tokens=len(batch) * 2,
                provider_cost_usd=Decimal("0.001"),
            ),
        )

    service._translate_batch = fake_translate_batch  # type: ignore[method-assign]
    candidates = [make_candidate(index) for index in range(1, 6)]

    result = await service.translate(candidates)

    assert [item.id for item in result.items] == [candidate.id for candidate in candidates]
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 10
    assert result.usage.provider_cost_usd == Decimal("0.003")


@pytest.mark.asyncio
async def test_translate_aggregates_usage_when_one_batch_fails() -> None:
    service = object.__new__(AITranslationService)
    service.settings = SimpleNamespace(  # type: ignore[assignment]
        openai_batch_size=2,
        openai_concurrency=2,
    )

    async def fake_translate_batch(batch: list[AICandidate]) -> AITranslationResult:
        if batch[0].id == "v00003":
            raise AITranslationError(
                "structured output failed",
                OpenAIUsage(input_tokens=7, provider_cost_usd=Decimal("0.02")),
            )
        return AITranslationResult(
            items=[make_output(candidate) for candidate in batch],
            usage=OpenAIUsage(input_tokens=3, provider_cost_usd=Decimal("0.01")),
        )

    service._translate_batch = fake_translate_batch  # type: ignore[method-assign]

    with pytest.raises(AITranslationError) as raised:
        await service.translate([make_candidate(index) for index in range(1, 6)])

    # Two successful batches plus the failed batch's recorded usage are retained.
    assert raised.value.usage.input_tokens == 13
    assert raised.value.usage.provider_cost_usd == Decimal("0.04")
