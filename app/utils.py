from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from fastapi import HTTPException

from .models import AnalysisOptions, TranscriptBlock


def api_error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_transcript(
    blocks: list[TranscriptBlock],
    *,
    max_blocks: int,
    max_characters: int,
    max_video_minutes: int,
) -> list[dict[str, float | str]]:
    if len(blocks) > max_blocks:
        raise api_error(
            413,
            "transcript_too_large",
            f"Transcript contains more than {max_blocks} blocks.",
        )

    normalized: list[dict[str, float | str]] = []
    total_characters = 0
    last_signature: tuple[str, float] | None = None

    for block in blocks:
        text = normalize_space(block.text)
        if not text:
            continue

        start = round(float(block.start), 3)
        duration = round(max(0.0, float(block.duration)), 3)
        signature = (text, start)
        if signature == last_signature:
            continue

        total_characters += len(text)
        if total_characters > max_characters:
            raise api_error(
                413,
                "transcript_too_large",
                f"Transcript exceeds {max_characters} characters.",
            )

        normalized.append(
            {
                "text": text,
                "start": start,
                "duration": duration,
            }
        )
        last_signature = signature

    if not normalized:
        raise api_error(400, "empty_transcript", "No usable transcript text was supplied.")

    last_end = max(float(block["start"]) + float(block["duration"]) for block in normalized)
    if last_end > max_video_minutes * 60:
        raise api_error(
            413,
            "video_too_long",
            f"Transcript analysis supports videos up to {max_video_minutes} minutes.",
        )

    return normalized


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def calculate_transcript_hash(transcript: list[dict[str, float | str]]) -> str:
    return sha256_json(transcript)


def calculate_settings_hash(
    *,
    source_language: str,
    options: AnalysisOptions,
    model: str,
    prompt_version: str,
    extractor_version: str,
) -> str:
    return sha256_json(
        {
            "source_language": source_language,
            "target_language": options.target_language,
            "vocabulary_types": sorted(options.vocabulary_types),
            "include_example": options.include_example,
            "model": model,
            "prompt_version": prompt_version,
            "extractor_version": extractor_version,
        }
    )


def calculate_billable_seconds(transcript: list[dict[str, float | str]]) -> int:
    last_end = max(float(block["start"]) + float(block["duration"]) for block in transcript)
    return int(math.ceil(last_end / 60.0) * 60)
