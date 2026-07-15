from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Header

from .config import Settings, get_settings
from .utils import api_error


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None


class SupabaseAuthVerifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.supabase_request_timeout_seconds)
        self._cache: dict[str, tuple[float, AuthenticatedUser]] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def verify(self, token: str) -> AuthenticatedUser:
        cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]

        if not self.settings.supabase_url or not self.settings.supabase_publishable_key:
            raise api_error(503, "auth_not_configured", "Supabase Auth is not configured.")

        response = await self._client.get(
            f"{self.settings.supabase_url}/auth/v1/user",
            headers={
                "apikey": self.settings.supabase_publishable_key,
                "Authorization": f"Bearer {token}",
            },
        )
        if response.status_code != 200:
            raise api_error(401, "invalid_session", "Your session is invalid or expired.")

        payload: dict[str, Any] = response.json()
        user_id = str(payload.get("id") or "").strip()
        if not user_id:
            raise api_error(401, "invalid_session", "Supabase returned no user ID.")

        user = AuthenticatedUser(
            id=user_id,
            email=payload.get("email"),
        )
        async with self._lock:
            if len(self._cache) > 1000:
                self._cache = {key: value for key, value in self._cache.items() if value[0] > now}
            self._cache[cache_key] = (now + 60.0, user)
        return user


_auth_verifier: SupabaseAuthVerifier | None = None


def get_auth_verifier() -> SupabaseAuthVerifier:
    global _auth_verifier
    if _auth_verifier is None:
        _auth_verifier = SupabaseAuthVerifier(get_settings())
    return _auth_verifier


async def close_auth_verifier() -> None:
    global _auth_verifier
    if _auth_verifier is not None:
        await _auth_verifier.close()
        _auth_verifier = None


async def require_user(authorization: str = Header(default="")) -> AuthenticatedUser:
    if not authorization.startswith("Bearer "):
        raise api_error(401, "authentication_required", "Sign in to use this feature.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise api_error(401, "authentication_required", "Sign in to use this feature.")
    return await get_auth_verifier().verify(token)
