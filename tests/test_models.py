from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import AIOutputItem, AnalysisOptions


def test_ai_noun_requires_article() -> None:
    with pytest.raises(ValidationError):
        AIOutputItem(
            id="v00001",
            type="noun",
            word="Entscheidung",
            translation="decision",
            example="Das war eine Entscheidung.",
            example_translation="That was a decision.",
            level="B1",
        )


def test_ai_verb_forces_null_article() -> None:
    item = AIOutputItem(
        id="v00002",
        type="verb",
        word="überzeugen",
        translation="to convince",
        article="der",
        example="Sie überzeugt ihn.",
        example_translation="She convinces him.",
        level="B1",
    )
    assert item.article is None


def test_analysis_options_deduplicate_types() -> None:
    options = AnalysisOptions(vocabulary_types=["noun", "noun", "verb"])
    assert options.vocabulary_types == ["noun", "verb"]
