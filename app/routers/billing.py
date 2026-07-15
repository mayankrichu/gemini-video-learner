from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import stripe
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse

from ..auth import AuthenticatedUser, require_user
from ..config import Settings, get_settings
from ..database import SupabaseDatabase, get_database
from ..models import CheckoutRequest, CheckoutResponse
from ..utils import api_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/billing", tags=["billing"])

StripeSignatureVerificationError = getattr(
    stripe,
    "SignatureVerificationError",
    getattr(getattr(stripe, "error", object()), "SignatureVerificationError", Exception),
)


def _stripe_object_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _checkout_success_url(settings: Settings) -> str:
    # Stripe replaces this literal placeholder after Checkout completes.
    return f"{settings.public_base_url}/v1/billing/success?session_id={{CHECKOUT_SESSION_ID}}"


@router.post("/checkout-session", response_model=CheckoutResponse)
async def create_checkout_session(
    payload: CheckoutRequest,
    user: AuthenticatedUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
    database: SupabaseDatabase = Depends(get_database),
) -> CheckoutResponse:
    if not settings.stripe_secret_key:
        raise api_error(503, "stripe_not_configured", "Stripe is not configured yet.")

    package = settings.stripe_packages[payload.package]
    price_id = str(package["price_id"] or "")
    if not price_id:
        raise api_error(
            503,
            "stripe_price_not_configured",
            f"The {payload.package}-minute Stripe price is not configured.",
        )

    profile = await database.ensure_profile(user.id, user.email)
    if profile.get("billing_mode") in {"at_cost", "free_admin"}:
        raise api_error(
            400,
            "internal_account",
            "This internal account uses OpenAI at cost and does not need minute packs.",
        )

    stripe.api_key = settings.stripe_secret_key
    customer_id = profile.get("stripe_customer_id")
    if not customer_id:
        customer = await asyncio.to_thread(
            stripe.Customer.create,
            email=user.email,
            metadata={"user_id": user.id},
        )
        customer_id = customer.id
        await database.update_profile(user.id, {"stripe_customer_id": customer_id})

    session_params: dict[str, Any] = {
        "mode": "payment",
        "customer": customer_id,
        "client_reference_id": user.id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": _checkout_success_url(settings),
        "cancel_url": f"{settings.public_base_url}/v1/billing/cancel",
        "metadata": {
            "user_id": user.id,
            "package": payload.package,
        },
        "billing_address_collection": "auto",
        "locale": "auto",
    }
    if settings.stripe_automatic_tax:
        session_params["automatic_tax"] = {"enabled": True}
        session_params["customer_update"] = {"address": "auto", "name": "auto"}

    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        **session_params,
    )
    if not session.url:
        raise api_error(502, "stripe_error", "Stripe returned no Checkout URL.")
    return CheckoutResponse(checkout_url=session.url)


@router.post("/stripe-webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
    settings: Settings = Depends(get_settings),
    database: SupabaseDatabase = Depends(get_database),
) -> dict[str, bool]:
    settings.require_stripe()
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload,
            stripe_signature,
            settings.stripe_webhook_secret,
        )
    except ValueError as exc:
        raise api_error(400, "invalid_webhook_payload", "Invalid Stripe payload.") from exc
    except StripeSignatureVerificationError as exc:
        raise api_error(400, "invalid_webhook_signature", "Invalid Stripe signature.") from exc

    event_type = event["type"]
    event_object = event["data"]["object"]

    if event_type == "charge.refunded":
        if not bool(_stripe_object_value(event_object, "refunded", False)):
            logger.warning(
                "Partial refund %s requires manual credit adjustment.",
                event["id"],
            )
            return {"received": True}
        payment_intent = _stripe_object_value(event_object, "payment_intent")
        payment_intent_id = str(_stripe_object_value(payment_intent, "id") or payment_intent or "")
        if payment_intent_id:
            await database.process_stripe_credit_reversal(
                event_id=str(event["id"]),
                event_type=event_type,
                payment_intent_id=payment_intent_id,
                reason="full_refund",
            )
        return {"received": True}

    if event_type not in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }:
        return {"received": True}

    session = event_object
    if _stripe_object_value(session, "payment_status") != "paid":
        return {"received": True}

    stripe.api_key = settings.stripe_secret_key
    session_id = str(_stripe_object_value(session, "id") or "")
    expanded = await asyncio.to_thread(
        stripe.checkout.Session.retrieve,
        session_id,
        expand=["line_items"],
    )
    metadata = dict(_stripe_object_value(expanded, "metadata") or {})
    package_key = str(metadata.get("package") or "")
    user_id = str(metadata.get("user_id") or "")
    package = settings.stripe_packages.get(package_key)
    if not package or not user_id:
        raise api_error(400, "invalid_checkout_metadata", "Checkout metadata is invalid.")

    line_items = _stripe_object_value(expanded, "line_items")
    line_item_data = _stripe_object_value(line_items, "data", []) or []
    actual_price_id = ""
    if line_item_data:
        price = _stripe_object_value(line_item_data[0], "price")
        actual_price_id = str(_stripe_object_value(price, "id") or "")
    expected_price_id = str(package["price_id"] or "")
    if not expected_price_id or actual_price_id != expected_price_id:
        raise api_error(
            400, "checkout_price_mismatch", "Checkout price does not match the package."
        )

    payment_intent = _stripe_object_value(expanded, "payment_intent")
    payment_intent_id = (
        str(_stripe_object_value(payment_intent, "id") or payment_intent)
        if payment_intent
        else None
    )
    processed = await database.process_stripe_checkout(
        event_id=str(event["id"]),
        event_type=event_type,
        user_id=user_id,
        seconds=int(package["seconds"]),
        checkout_session_id=session_id,
        payment_intent_id=payment_intent_id,
        expiration_days=settings.credit_expiration_days,
    )
    logger.info(
        "Stripe event %s processed=%s package=%s user=%s",
        event["id"],
        processed,
        package_key,
        user_id,
    )
    return {"received": True}


@router.get("/success", response_class=HTMLResponse)
async def checkout_success(session_id: str = "") -> str:
    safe_session = html.escape(session_id)
    return f"""
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>Payment complete</title></head>
      <body style="font-family:Arial,sans-serif;max-width:620px;margin:80px auto;padding:24px;">
        <h1>Payment complete</h1>
        <p>Your AI minutes will appear in the extension after the Stripe webhook is processed.</p>
        <p>You can close this tab and press <strong>Refresh balance</strong> in the extension.</p>
        <small>Session: {safe_session}</small>
      </body>
    </html>
    """


@router.get("/cancel", response_class=HTMLResponse)
async def checkout_cancel() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>Payment cancelled</title></head>
      <body style="font-family:Arial,sans-serif;max-width:620px;margin:80px auto;padding:24px;">
        <h1>Payment cancelled</h1>
        <p>No charge was made. You can close this tab.</p>
      </body>
    </html>
    """
