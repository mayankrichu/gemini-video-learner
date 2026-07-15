from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from .config import Settings, get_settings
from .models import AIOutputBatch, AIOutputItem
from .pricing import calculate_provider_cost_usd
from .vocab import AICandidate

SYSTEM_PROMPT = """You create concise German-to-English vocabulary data for language learners.
The input contains German nouns and verbs already extracted from a timestamped transcript.
For every input ID, return exactly one output item with the same ID and type.

Rules:
- Translate the word according to the supplied German context.
- For nouns, return the singular nominative noun and exactly one article: der, die, or das.
- For verbs, return the infinitive and set article to null.
- Give one natural, short German example sentence using the word.
- Give an accurate English translation of that example.
- Use learner-friendly English, without commentary or alternatives.
- Do not invent, remove, merge, reorder, or duplicate IDs.
- Never include timestamps; the server preserves timestamps independently.
"""


@dataclass
class OpenAIUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    provider_cost_usd: Decimal = Decimal("0")

    def add(self, other: OpenAIUsage) -> None:
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.provider_cost_usd += other.provider_cost_usd


@dataclass
class AITranslationResult:
    items: list[AIOutputItem]
    usage: OpenAIUsage


class AITranslationError(RuntimeError):
    def __init__(self, message: str, usage: OpenAIUsage | None = None):
        super().__init__(message)
        self.usage = usage or OpenAIUsage()


class AITranslationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.require_openai()
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )

    async def translate(self, candidates: list[AICandidate]) -> AITranslationResult:
        if not candidates:
            return AITranslationResult(items=[], usage=OpenAIUsage())

        batches = [
            candidates[start : start + self.settings.openai_batch_size]
            for start in range(0, len(candidates), self.settings.openai_batch_size)
        ]
        semaphore = asyncio.Semaphore(self.settings.openai_concurrency)

        async def run_batch(
            index: int,
            batch: list[AICandidate],
        ) -> tuple[int, AITranslationResult | None, AITranslationError | None]:
            async with semaphore:
                try:
                    return index, await self._translate_batch(batch), None
                except AITranslationError as exc:
                    return index, None, exc
                except Exception as exc:
                    return index, None, AITranslationError(str(exc))

        completed = await asyncio.gather(
            *(run_batch(index, batch) for index, batch in enumerate(batches))
        )

        results_by_index: dict[int, AITranslationResult] = {}
        errors: list[AITranslationError] = []
        total_usage = OpenAIUsage()

        for index, result, error in completed:
            if result is not None:
                results_by_index[index] = result
                total_usage.add(result.usage)
            elif error is not None:
                errors.append(error)
                total_usage.add(error.usage)

        if errors:
            first_error = str(errors[0]) or "Unknown OpenAI batch error."
            raise AITranslationError(
                f"{len(errors)} of {len(batches)} OpenAI batches failed. "
                f"First error: {first_error}",
                total_usage,
            )

        all_items = [
            item for index in range(len(batches)) for item in results_by_index[index].items
        ]

        return AITranslationResult(items=all_items, usage=total_usage)

    async def _translate_batch(self, candidates: list[AICandidate]) -> AITranslationResult:
        payload = {
            "source_language": "de",
            "target_language": "en",
            "items": [candidate.prompt_payload() for candidate in candidates],
        }
        response = await self.client.responses.parse(
            model=self.settings.openai_model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            text_format=AIOutputBatch,
            max_output_tokens=7000,
            store=False,
        )
        batch_usage = self._usage_from_response(response)
        try:
            parsed = response.output_parsed
            if parsed is None:
                raise RuntimeError("OpenAI returned no structured vocabulary result.")

            expected_ids = [candidate.id for candidate in candidates]
            returned_ids = [item.id for item in parsed.items]
            if len(returned_ids) != len(set(returned_ids)):
                raise RuntimeError("OpenAI returned duplicate vocabulary IDs.")
            if set(returned_ids) != set(expected_ids):
                raise RuntimeError("OpenAI returned an incomplete or mismatched vocabulary batch.")

            by_id = {item.id: item for item in parsed.items}
            for candidate in candidates:
                if by_id[candidate.id].type != candidate.type:
                    raise RuntimeError(f"OpenAI changed vocabulary type for {candidate.id}.")
            ordered = [by_id[item_id] for item_id in expected_ids]
            return AITranslationResult(items=ordered, usage=batch_usage)
        except AITranslationError:
            raise
        except Exception as exc:
            raise AITranslationError(str(exc), batch_usage) from exc

    def _usage_from_response(self, response: Any) -> OpenAIUsage:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        cached_tokens = min(cached_tokens, input_tokens)
        cost = calculate_provider_cost_usd(
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            input_usd_per_million=self.settings.openai_input_usd_per_million,
            cached_input_usd_per_million=(self.settings.openai_cached_input_usd_per_million),
            output_usd_per_million=self.settings.openai_output_usd_per_million,
        )
        return OpenAIUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            provider_cost_usd=cost,
        )


@lru_cache(maxsize=1)
def get_ai_service() -> AITranslationService:
    return AITranslationService(get_settings())
