from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

VocabularyType = Literal["noun", "verb", "connector"]
BillingMode = Literal["prepaid", "at_cost", "free_admin"]


class TranscriptBlock(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    start: float = Field(..., ge=0)
    duration: float = Field(..., ge=0, le=120)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split()).strip()
        if not normalized:
            raise ValueError("Transcript text cannot be empty.")
        return normalized


class AnalysisOptions(BaseModel):
    use_ai: bool = False
    target_language: Literal["en"] = "en"
    vocabulary_types: list[VocabularyType] = Field(default_factory=lambda: ["noun", "verb"])
    include_example: bool = True

    @field_validator("vocabulary_types")
    @classmethod
    def normalize_types(cls, value: list[VocabularyType]) -> list[VocabularyType]:
        normalized: list[VocabularyType] = []
        for item in value:
            if item not in normalized:
                normalized.append(item)
        if not normalized:
            raise ValueError("Select at least one vocabulary type.")
        return normalized


class TranscriptAnalyzeRequest(BaseModel):
    request_id: UUID = Field(default_factory=uuid4)
    video_id: str = Field(..., pattern=r"^[0-9A-Za-z_-]{11}$")
    source_language: str = Field(default="de", min_length=2, max_length=20)
    transcript: list[TranscriptBlock] = Field(..., min_length=1)
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)

    @field_validator("source_language")
    @classmethod
    def normalize_source_language(cls, value: str) -> str:
        return value.strip().lower().replace("_", "-")


class VocabItem(BaseModel):
    start: float
    end: float
    type: VocabularyType
    word: str
    translation: str
    article: str | None = None
    example: str | None = None
    example_translation: str | None = None
    source_text: str | None = None
    ai_generated: bool = False
    level: str = "B1+"


class AnalysisResponse(BaseModel):
    items: list[VocabItem]
    request_id: UUID
    video_id: str
    mode: Literal["free", "ai"]
    charged_seconds: int = 0
    charged_minutes: int = 0
    billable_seconds: int = 0
    remaining_seconds: int | None = None
    remaining_minutes: int | None = None
    billing_mode: BillingMode | None = None
    reused_entitlement: bool = False
    cache_hit: bool = False
    provider_cost_usd: float = 0.0
    provider_cost_eur_estimate: float = 0.0


class CheckoutRequest(BaseModel):
    package: Literal["100", "500", "1000"]


class CheckoutResponse(BaseModel):
    checkout_url: str


class DevCreditRequest(BaseModel):
    minutes: int = Field(..., ge=1, le=10000)


class AccountSummary(BaseModel):
    user_id: UUID
    email: str | None = None
    billing_mode: BillingMode
    is_admin: bool = False
    remaining_seconds: int | None = None
    remaining_minutes: int | None = None
    next_expiration: datetime | None = None
    current_month_provider_cost_usd: float = 0.0
    current_month_provider_cost_eur_estimate: float = 0.0
    current_month_video_minutes: float = 0.0
    current_month_videos: int = 0
    monthly_cost_limit_usd: float | None = None


class AIOutputItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=100)
    type: Literal["noun", "verb"]
    word: str = Field(..., min_length=1, max_length=120)
    translation: str = Field(..., min_length=1, max_length=300)
    article: Literal["der", "die", "das"] | None = None
    example: str = Field(..., min_length=1, max_length=500)
    example_translation: str = Field(..., min_length=1, max_length=500)
    level: Literal["A1", "A2", "B1", "B2", "C1", "C2"] = "B1"

    @model_validator(mode="after")
    def validate_article(self) -> AIOutputItem:
        if self.type == "noun" and self.article is None:
            raise ValueError("A German noun must include der, die, or das.")
        if self.type == "verb":
            self.article = None
        return self


class AIOutputBatch(BaseModel):
    items: list[AIOutputItem]
