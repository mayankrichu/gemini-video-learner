from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "German Vocab Overlay API"
    app_version: str = "2.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    allowed_origins: str = ""
    allowed_origin_regex: str = ""
    free_rate_limit: str = "30/minute"
    ai_rate_limit: str = "6/minute"

    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""
    supabase_request_timeout_seconds: float = 20.0

    azure_storage_account_name: str = "nakshavastorageaccount"
    azure_blob_container_name: str = "vocab-data"
    azure_storage_connection_string: str = ""
    local_dictionary_dir: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_prompt_version: str = "v1"
    openai_timeout_seconds: float = Field(default=75.0, gt=0, le=180)
    openai_max_retries: int = Field(default=1, ge=0, le=3)
    openai_batch_size: int = Field(default=50, ge=10, le=100)
    openai_concurrency: int = Field(default=3, ge=1, le=5)
    openai_input_usd_per_million: float = Field(default=0.25, ge=0)
    openai_cached_input_usd_per_million: float = Field(default=0.025, ge=0)
    openai_output_usd_per_million: float = Field(default=2.0, ge=0)
    openai_usd_to_eur_rate: float = Field(default=1.0, gt=0)

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_100_min: str = ""
    stripe_price_500_min: str = ""
    stripe_price_1000_min: str = ""
    stripe_automatic_tax: bool = False
    credit_expiration_days: int = Field(default=365, ge=1, le=3650)

    admin_email: str = ""
    enable_dev_credit_endpoint: bool = False

    # Keep synchronous App Service requests comfortably below Azure's front-end
    # request timeout. Credit packs may contain more minutes than one video.
    max_video_minutes: int = Field(default=180, ge=1, le=2000)
    max_transcript_blocks: int = Field(default=25000, ge=100, le=100000)
    max_transcript_characters: int = Field(default=1500000, ge=10000, le=5000000)
    max_ai_candidates: int = Field(default=300, ge=10, le=10000)
    reservation_ttl_minutes: int = Field(default=60, ge=5, le=180)
    entitlement_stale_minutes: int = Field(default=75, ge=5, le=240)

    @field_validator(
        "public_base_url",
        "supabase_url",
        mode="before",
    )
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return str(value or "").strip().rstrip("/")

    @property
    def allowed_origins_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def stripe_packages(self) -> dict[str, dict[str, int | str]]:
        return {
            "100": {
                "minutes": 100,
                "seconds": 100 * 60,
                "price_id": self.stripe_price_100_min,
            },
            "500": {
                "minutes": 500,
                "seconds": 500 * 60,
                "price_id": self.stripe_price_500_min,
            },
            "1000": {
                "minutes": 1000,
                "seconds": 1000 * 60,
                "price_id": self.stripe_price_1000_min,
            },
        }

    def require_supabase(self) -> None:
        missing = [
            name
            for name, value in {
                "SUPABASE_URL": self.supabase_url,
                "SUPABASE_PUBLISHABLE_KEY": self.supabase_publishable_key,
                "SUPABASE_SECRET_KEY": self.supabase_secret_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Supabase settings: {', '.join(missing)}")

    def require_openai(self) -> None:
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

    def require_stripe(self) -> None:
        missing = [
            name
            for name, value in {
                "STRIPE_SECRET_KEY": self.stripe_secret_key,
                "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Stripe settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
