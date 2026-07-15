from __future__ import annotations

import pytest

from app.vocab import AICandidateLimitError, VocabService


class FakeToken:
    def __init__(self, lemma: str):
        self.lemma_ = lemma
        self.pos_ = "NOUN"
        self.is_alpha = True
        self.is_stop = False


class FakeNLP:
    def pipe(self, texts: list[str], batch_size: int):
        del batch_size
        for text in texts:
            yield [FakeToken(text.lower())]


def test_ai_candidate_limit_rejects_instead_of_silently_truncating() -> None:
    service = object.__new__(VocabService)
    service.nlp = FakeNLP()
    transcript = [
        {"text": "Entscheidung", "start": 0.0, "duration": 1.0},
        {"text": "Verantwortung", "start": 1.0, "duration": 1.0},
        {"text": "Entwicklung", "start": 2.0, "duration": 1.0},
    ]

    with pytest.raises(AICandidateLimitError) as raised:
        service.extract_ai_candidates(transcript, ["noun"], max_candidates=2)

    assert raised.value.limit == 2
