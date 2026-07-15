from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models import AnalysisOptions, TranscriptAnalyzeRequest, TranscriptBlock
from app.utils import (
    calculate_billable_seconds,
    calculate_settings_hash,
    calculate_transcript_hash,
    normalize_transcript,
)


def test_normalize_transcript_collapses_space_and_adjacent_duplicates() -> None:
    blocks = [
        TranscriptBlock(text="  Das   ist wichtig. ", start=0, duration=2.4),
        TranscriptBlock(text="Das ist wichtig.", start=0, duration=2.4),
        TranscriptBlock(text="Eine Entscheidung", start=61.1, duration=1.2),
    ]

    normalized = normalize_transcript(
        blocks,
        max_blocks=100,
        max_characters=10_000,
        max_video_minutes=10,
    )

    assert normalized == [
        {"text": "Das ist wichtig.", "start": 0.0, "duration": 2.4},
        {"text": "Eine Entscheidung", "start": 61.1, "duration": 1.2},
    ]
    assert calculate_billable_seconds(normalized) == 120


def test_transcript_hash_is_stable() -> None:
    transcript = [{"text": "Hallo", "start": 1.0, "duration": 2.0}]
    assert calculate_transcript_hash(transcript) == calculate_transcript_hash(transcript)
    assert calculate_transcript_hash(transcript) != calculate_transcript_hash(
        [{"text": "Tschüss", "start": 1.0, "duration": 2.0}]
    )


def test_settings_hash_ignores_vocabulary_type_order() -> None:
    first = calculate_settings_hash(
        source_language="de",
        options=AnalysisOptions(vocabulary_types=["noun", "verb"]),
        model="gpt-5-mini",
        prompt_version="v1",
        extractor_version="v2",
    )
    second = calculate_settings_hash(
        source_language="de",
        options=AnalysisOptions(vocabulary_types=["verb", "noun"]),
        model="gpt-5-mini",
        prompt_version="v1",
        extractor_version="v2",
    )
    assert first == second


def test_request_requires_youtube_video_id() -> None:
    with pytest.raises(ValidationError):
        TranscriptAnalyzeRequest(
            request_id=uuid4(),
            video_id="not-a-youtube-id",
            transcript=[TranscriptBlock(text="Hallo", start=0, duration=1)],
        )
