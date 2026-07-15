from __future__ import annotations

from app.config import Settings
from app.routers.billing import _checkout_success_url


def test_checkout_success_url_contains_exact_stripe_placeholder() -> None:
    settings = Settings(public_base_url="https://example.test/")
    assert _checkout_success_url(settings) == (
        "https://example.test/v1/billing/success?session_id={CHECKOUT_SESSION_ID}"
    )
