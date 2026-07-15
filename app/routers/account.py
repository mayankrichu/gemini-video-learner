from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..auth import AuthenticatedUser, require_user
from ..config import Settings, get_settings
from ..database import SupabaseDatabase, get_database
from ..models import AccountSummary, DevCreditRequest
from ..utils import api_error

router = APIRouter(prefix="/v1", tags=["account"])


def snapshot_to_model(snapshot: dict, user: AuthenticatedUser) -> AccountSummary:
    return AccountSummary(
        user_id=user.id,
        email=snapshot.get("email") or user.email,
        billing_mode=snapshot.get("billing_mode", "prepaid"),
        is_admin=bool(snapshot.get("is_admin", False)),
        remaining_seconds=snapshot.get("remaining_seconds"),
        remaining_minutes=snapshot.get("remaining_minutes"),
        next_expiration=snapshot.get("next_expiration"),
        current_month_provider_cost_usd=float(snapshot.get("current_month_provider_cost_usd") or 0),
        current_month_provider_cost_eur_estimate=float(
            snapshot.get("current_month_provider_cost_eur_estimate") or 0
        ),
        current_month_video_minutes=float(snapshot.get("current_month_video_minutes") or 0),
        current_month_videos=int(snapshot.get("current_month_videos") or 0),
        monthly_cost_limit_usd=(
            float(snapshot["monthly_cost_limit_usd"])
            if snapshot.get("monthly_cost_limit_usd") is not None
            else None
        ),
    )


@router.get("/account/summary", response_model=AccountSummary)
async def account_summary(
    user: AuthenticatedUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
    database: SupabaseDatabase = Depends(get_database),
) -> AccountSummary:
    await database.ensure_profile(user.id, user.email)
    snapshot = await database.account_snapshot(
        user.id,
        settings.openai_usd_to_eur_rate,
    )
    return snapshot_to_model(snapshot, user)


@router.post("/admin/dev-credits", response_model=AccountSummary)
async def grant_development_credits(
    payload: DevCreditRequest,
    user: AuthenticatedUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
    database: SupabaseDatabase = Depends(get_database),
) -> AccountSummary:
    if settings.environment == "production" or not settings.enable_dev_credit_endpoint:
        raise api_error(404, "not_found", "This endpoint is disabled.")

    profile = await database.ensure_profile(user.id, user.email)
    admin_email_matches = bool(
        settings.admin_email and user.email and user.email.lower() == settings.admin_email.lower()
    )
    if not profile.get("is_admin") and not admin_email_matches:
        raise api_error(403, "admin_required", "Administrator access is required.")

    await database.grant_dev_credits(
        user.id,
        payload.minutes,
        settings.credit_expiration_days,
    )
    snapshot = await database.account_snapshot(
        user.id,
        settings.openai_usd_to_eur_rate,
    )
    return snapshot_to_model(snapshot, user)


@router.get("/account/confirmed", response_class=HTMLResponse)
async def account_confirmed() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Email confirmed</title>
      </head>
      <body style="font-family:Arial,sans-serif;max-width:620px;margin:80px auto;padding:24px;">
        <h1>Email confirmed</h1>
        <p>Your German Vocab account is ready. Return to the extension and sign in.</p>
        <script>
          if (location.hash) history.replaceState(null, "", location.pathname);
        </script>
      </body>
    </html>
    """
