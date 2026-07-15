from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class SupabaseDatabaseError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SupabaseDatabase:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.supabase_request_timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.supabase_url or not self.settings.supabase_secret_key:
            raise RuntimeError("Supabase database settings are not configured.")
        secret_key = self.settings.supabase_secret_key
        headers = {
            "apikey": secret_key,
            "Content-Type": "application/json",
        }
        # New sb_secret_ keys are API keys, not JWTs, and must not be placed in
        # Authorization. Keep legacy service_role JWTs working during migration.
        if secret_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {secret_key}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        prefer: str | None = None,
    ) -> Any:
        headers = self.headers.copy()
        if prefer:
            headers["Prefer"] = prefer
        response = await self._client.request(
            method,
            f"{self.settings.supabase_url}{path}",
            params=params,
            json=json_body,
            headers=headers,
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            message = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise SupabaseDatabaseError(
                response.status_code, message or "Database request failed.", payload
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def rpc(self, function_name: str, payload: dict[str, Any]) -> Any:
        return await self._request(
            "POST",
            f"/rest/v1/rpc/{function_name}",
            json_body=payload,
        )

    async def ensure_profile(self, user_id: str, email: str | None) -> dict[str, Any]:
        await self._request(
            "POST",
            "/rest/v1/profiles",
            params={"on_conflict": "id"},
            json_body={"id": user_id, "email": email},
            prefer="resolution=merge-duplicates,return=representation",
        )
        return await self.get_profile(user_id)

    async def get_profile(self, user_id: str) -> dict[str, Any]:
        rows = await self._request(
            "GET",
            "/rest/v1/profiles",
            params={
                "id": f"eq.{user_id}",
                "select": "id,email,billing_mode,is_admin,monthly_cost_limit_usd,stripe_customer_id",
                "limit": "1",
            },
        )
        if not rows:
            raise SupabaseDatabaseError(404, "Profile not found.")
        return rows[0]

    async def update_profile(self, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        rows = await self._request(
            "PATCH",
            "/rest/v1/profiles",
            params={"id": f"eq.{user_id}"},
            json_body=values,
            prefer="return=representation",
        )
        if not rows:
            raise SupabaseDatabaseError(404, "Profile not found.")
        return rows[0]

    async def account_snapshot(self, user_id: str, usd_to_eur_rate: float) -> dict[str, Any]:
        result = await self.rpc(
            "get_account_snapshot",
            {
                "p_user_id": user_id,
                "p_usd_to_eur_rate": usd_to_eur_rate,
            },
        )
        return self._unwrap(result)

    async def claim_entitlement(
        self,
        *,
        user_id: str,
        video_id: str,
        transcript_hash: str,
        settings_hash: str,
        request_id: str,
        stale_after_seconds: int,
    ) -> dict[str, Any]:
        result = await self.rpc(
            "claim_video_entitlement",
            {
                "p_user_id": user_id,
                "p_video_id": video_id,
                "p_transcript_hash": transcript_hash,
                "p_settings_hash": settings_hash,
                "p_request_id": request_id,
                "p_stale_after_seconds": stale_after_seconds,
            },
        )
        return self._unwrap(result)

    async def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "/rest/v1/ai_analyses",
            params={
                "id": f"eq.{analysis_id}",
                "select": "*",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def find_analysis(
        self,
        *,
        video_id: str,
        transcript_hash: str,
        settings_hash: str,
        prompt_version: str,
        model: str,
    ) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "/rest/v1/ai_analyses",
            params={
                "video_id": f"eq.{video_id}",
                "transcript_hash": f"eq.{transcript_hash}",
                "settings_hash": f"eq.{settings_hash}",
                "prompt_version": f"eq.{prompt_version}",
                "model": f"eq.{model}",
                "select": "*",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def upsert_analysis(self, payload: dict[str, Any]) -> str:
        result = await self.rpc("upsert_ai_analysis", payload)
        value = self._unwrap(result)
        return str(value)

    async def reserve_credits(
        self,
        *,
        user_id: str,
        request_id: str,
        video_id: str,
        transcript_hash: str,
        settings_hash: str,
        required_seconds: int,
        ttl_minutes: int,
    ) -> str:
        result = await self.rpc(
            "reserve_credits",
            {
                "p_user_id": user_id,
                "p_request_id": request_id,
                "p_video_id": video_id,
                "p_transcript_hash": transcript_hash,
                "p_settings_hash": settings_hash,
                "p_required_seconds": required_seconds,
                "p_ttl_minutes": ttl_minutes,
            },
        )
        return str(self._unwrap(result))

    async def finalize_purchase(
        self,
        *,
        reservation_id: str,
        entitlement_id: str,
        request_id: str,
        analysis_id: str,
        charged_seconds: int,
    ) -> None:
        await self.rpc(
            "finalize_analysis_purchase",
            {
                "p_reservation_id": reservation_id,
                "p_entitlement_id": entitlement_id,
                "p_request_id": request_id,
                "p_analysis_id": analysis_id,
                "p_charged_seconds": charged_seconds,
            },
        )

    async def release_reservation(self, reservation_id: str) -> None:
        await self.rpc(
            "release_credit_reservation",
            {"p_reservation_id": reservation_id},
        )

    async def complete_entitlement(
        self,
        *,
        entitlement_id: str,
        request_id: str,
        analysis_id: str,
        charged_seconds: int,
    ) -> None:
        await self.rpc(
            "complete_video_entitlement",
            {
                "p_entitlement_id": entitlement_id,
                "p_request_id": request_id,
                "p_analysis_id": analysis_id,
                "p_charged_seconds": charged_seconds,
            },
        )

    async def fail_entitlement(self, entitlement_id: str, request_id: str) -> None:
        await self.rpc(
            "fail_video_entitlement",
            {
                "p_entitlement_id": entitlement_id,
                "p_request_id": request_id,
            },
        )

    async def upsert_usage_event(self, payload: dict[str, Any]) -> None:
        await self._request(
            "POST",
            "/rest/v1/usage_events",
            params={"on_conflict": "request_id"},
            json_body=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    async def grant_dev_credits(self, user_id: str, minutes: int, expiration_days: int) -> str:
        result = await self.rpc(
            "grant_manual_credits",
            {
                "p_user_id": user_id,
                "p_seconds": minutes * 60,
                "p_expiration_days": expiration_days,
                "p_source": "development",
            },
        )
        return str(self._unwrap(result))

    async def process_stripe_checkout(
        self,
        *,
        event_id: str,
        event_type: str,
        user_id: str,
        seconds: int,
        checkout_session_id: str,
        payment_intent_id: str | None,
        expiration_days: int,
    ) -> bool:
        result = await self.rpc(
            "process_stripe_checkout",
            {
                "p_event_id": event_id,
                "p_event_type": event_type,
                "p_user_id": user_id,
                "p_seconds": seconds,
                "p_checkout_session_id": checkout_session_id,
                "p_payment_intent_id": payment_intent_id,
                "p_expiration_days": expiration_days,
            },
        )
        return bool(self._unwrap(result))

    async def process_stripe_credit_reversal(
        self,
        *,
        event_id: str,
        event_type: str,
        payment_intent_id: str,
        reason: str,
    ) -> bool:
        result = await self.rpc(
            "process_stripe_credit_reversal",
            {
                "p_event_id": event_id,
                "p_event_type": event_type,
                "p_payment_intent_id": payment_intent_id,
                "p_reason": reason,
            },
        )
        return bool(self._unwrap(result))

    @staticmethod
    def _unwrap(value: Any) -> Any:
        if isinstance(value, list) and len(value) == 1:
            only = value[0]
            if isinstance(only, dict) and len(only) == 1:
                return next(iter(only.values()))
            return only
        return value


_database: SupabaseDatabase | None = None


def get_database() -> SupabaseDatabase:
    global _database
    if _database is None:
        _database = SupabaseDatabase(get_settings())
    return _database


async def close_database() -> None:
    global _database
    if _database is not None:
        await _database.close()
        _database = None
