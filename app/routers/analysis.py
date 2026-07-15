from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..ai_service import (
    AITranslationError,
    AITranslationResult,
    OpenAIUsage,
    get_ai_service,
)
from ..auth import AuthenticatedUser, require_user
from ..config import Settings, get_settings
from ..database import (
    SupabaseDatabase,
    SupabaseDatabaseError,
    get_database,
)
from ..models import AnalysisResponse, TranscriptAnalyzeRequest, VocabItem
from ..rate_limit import limiter
from ..utils import (
    api_error,
    calculate_billable_seconds,
    calculate_settings_hash,
    calculate_transcript_hash,
    normalize_transcript,
)
from ..vocab import (
    EXTRACTOR_VERSION,
    AICandidateLimitError,
    VocabService,
    get_vocab_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/analyze", tags=["analysis"])


def _validate_german(source_language: str) -> None:
    if not source_language.startswith("de"):
        raise api_error(
            400,
            "german_captions_required",
            "This version supports German captions translated to English only.",
        )


def _normalize_payload(
    payload: TranscriptAnalyzeRequest,
    settings: Settings,
) -> list[dict[str, float | str]]:
    _validate_german(payload.source_language)
    return normalize_transcript(
        payload.transcript,
        max_blocks=settings.max_transcript_blocks,
        max_characters=settings.max_transcript_characters,
        max_video_minutes=settings.max_video_minutes,
    )


def _analysis_response(
    *,
    payload: TranscriptAnalyzeRequest,
    items: list[dict[str, Any]],
    mode: str,
    billable_seconds: int,
    charged_seconds: int = 0,
    snapshot: dict[str, Any] | None = None,
    reused_entitlement: bool = False,
    cache_hit: bool = False,
    provider_cost_usd: Decimal = Decimal("0"),
    provider_cost_eur: Decimal = Decimal("0"),
) -> AnalysisResponse:
    return AnalysisResponse(
        items=[VocabItem.model_validate(item) for item in items],
        request_id=payload.request_id,
        video_id=payload.video_id,
        mode=mode,
        charged_seconds=charged_seconds,
        charged_minutes=(charged_seconds + 59) // 60,
        billable_seconds=billable_seconds,
        remaining_seconds=(snapshot or {}).get("remaining_seconds"),
        remaining_minutes=(snapshot or {}).get("remaining_minutes"),
        billing_mode=(snapshot or {}).get("billing_mode"),
        reused_entitlement=reused_entitlement,
        cache_hit=cache_hit,
        provider_cost_usd=float(provider_cost_usd),
        provider_cost_eur_estimate=float(provider_cost_eur),
    )


async def _safe_snapshot(
    database: SupabaseDatabase,
    user_id: str,
    settings: Settings,
) -> dict[str, Any] | None:
    try:
        return await database.account_snapshot(
            user_id,
            settings.openai_usd_to_eur_rate,
        )
    except Exception:
        logger.exception("Could not load account snapshot for %s", user_id)
        return None


async def _safe_usage_event(database: SupabaseDatabase, payload: dict[str, Any]) -> None:
    try:
        await database.upsert_usage_event(payload)
    except Exception:
        logger.exception("Could not persist usage event %s", payload.get("request_id"))


@router.post("/free", response_model=AnalysisResponse)
@limiter.limit(get_settings().free_rate_limit)
async def analyze_free(
    request: Request,
    payload: TranscriptAnalyzeRequest,
    settings: Settings = Depends(get_settings),
    vocab: VocabService = Depends(get_vocab_service),
) -> AnalysisResponse:
    transcript = _normalize_payload(payload, settings)
    selected = payload.options.vocabulary_types or ["noun", "verb", "connector"]
    items = vocab.analyze_free(transcript, selected)
    return _analysis_response(
        payload=payload,
        items=items,
        mode="free",
        billable_seconds=0,
    )


@router.post("/ai", response_model=AnalysisResponse)
@limiter.limit(get_settings().ai_rate_limit)
async def analyze_ai(
    request: Request,
    payload: TranscriptAnalyzeRequest,
    user: AuthenticatedUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
    database: SupabaseDatabase = Depends(get_database),
    vocab: VocabService = Depends(get_vocab_service),
) -> AnalysisResponse:
    settings.require_supabase()
    settings.require_openai()
    transcript = _normalize_payload(payload, settings)

    selected_types = [item for item in payload.options.vocabulary_types if item in {"noun", "verb"}]
    if not selected_types:
        raise api_error(
            400,
            "ai_types_required",
            "AI mode requires nouns, verbs, or both.",
        )

    transcript_hash = calculate_transcript_hash(transcript)
    settings_hash = calculate_settings_hash(
        source_language=payload.source_language,
        options=payload.options,
        model=settings.openai_model,
        prompt_version=settings.openai_prompt_version,
        extractor_version=EXTRACTOR_VERSION,
    )
    billable_seconds = calculate_billable_seconds(transcript)
    request_id = str(payload.request_id)

    profile = await database.ensure_profile(user.id, user.email)
    claim = await database.claim_entitlement(
        user_id=user.id,
        video_id=payload.video_id,
        transcript_hash=transcript_hash,
        settings_hash=settings_hash,
        request_id=request_id,
        stale_after_seconds=settings.entitlement_stale_minutes * 60,
    )
    entitlement_id = str(claim["id"])

    if claim.get("status") == "ready":
        analysis_id = claim.get("analysis_id")
        if not analysis_id:
            raise api_error(500, "invalid_entitlement", "Stored analysis is unavailable.")
        analysis = await database.get_analysis(str(analysis_id))
        if not analysis:
            raise api_error(500, "analysis_missing", "Stored analysis is unavailable.")
        snapshot = await _safe_snapshot(database, user.id, settings)
        await _safe_usage_event(
            database,
            {
                "user_id": user.id,
                "request_id": request_id,
                "video_id": payload.video_id,
                "transcript_hash": transcript_hash,
                "settings_hash": settings_hash,
                "analysis_id": analysis_id,
                "billing_mode": profile.get("billing_mode", "prepaid"),
                "billable_seconds": 0,
                "charged_seconds": 0,
                "model": settings.openai_model,
                "status": "completed",
                "cache_hit": True,
                "reused_entitlement": True,
                "provider_cost_usd": 0,
                "provider_cost_eur_estimate": 0,
            },
        )
        return _analysis_response(
            payload=payload,
            items=analysis.get("result") or [],
            mode="ai",
            billable_seconds=billable_seconds,
            charged_seconds=0,
            snapshot=snapshot,
            reused_entitlement=True,
            cache_hit=True,
        )

    if not claim.get("claimed"):
        raise api_error(
            409,
            "analysis_in_progress",
            "AI analysis for this video is already running. Try again shortly.",
        )

    await _safe_usage_event(
        database,
        {
            "user_id": user.id,
            "request_id": request_id,
            "video_id": payload.video_id,
            "transcript_hash": transcript_hash,
            "settings_hash": settings_hash,
            "billing_mode": profile.get("billing_mode", "prepaid"),
            "billable_seconds": billable_seconds,
            "charged_seconds": 0,
            "model": settings.openai_model,
            "status": "processing",
            "cache_hit": False,
            "reused_entitlement": False,
        },
    )

    reservation_id: str | None = None
    analysis_id: str | None = None
    cache_hit = False
    usage = OpenAIUsage()

    try:
        analysis = await database.find_analysis(
            video_id=payload.video_id,
            transcript_hash=transcript_hash,
            settings_hash=settings_hash,
            prompt_version=settings.openai_prompt_version,
            model=settings.openai_model,
        )
        cache_hit = analysis is not None

        candidates = None
        if analysis is None:
            try:
                candidates = vocab.extract_ai_candidates(
                    transcript,
                    selected_types,
                    settings.max_ai_candidates,
                )
            except AICandidateLimitError as exc:
                raise api_error(
                    413,
                    "too_many_ai_candidates",
                    (
                        "This transcript contains too many unique nouns and verbs for "
                        "synchronous AI analysis. Use a shorter video or split the video "
                        "into sections."
                    ),
                    max_candidates=exc.limit,
                ) from exc

            if not candidates:
                analysis_id = await database.upsert_analysis(
                    {
                        "p_video_id": payload.video_id,
                        "p_transcript_hash": transcript_hash,
                        "p_settings_hash": settings_hash,
                        "p_source_language": payload.source_language,
                        "p_target_language": payload.options.target_language,
                        "p_prompt_version": settings.openai_prompt_version,
                        "p_model": settings.openai_model,
                        "p_result": [],
                        "p_input_tokens": 0,
                        "p_cached_input_tokens": 0,
                        "p_output_tokens": 0,
                        "p_provider_cost_usd": 0,
                    }
                )
                await database.complete_entitlement(
                    entitlement_id=entitlement_id,
                    request_id=request_id,
                    analysis_id=analysis_id,
                    charged_seconds=0,
                )
                await _safe_usage_event(
                    database,
                    {
                        "user_id": user.id,
                        "request_id": request_id,
                        "video_id": payload.video_id,
                        "transcript_hash": transcript_hash,
                        "settings_hash": settings_hash,
                        "analysis_id": analysis_id,
                        "billing_mode": profile.get("billing_mode", "prepaid"),
                        "billable_seconds": billable_seconds,
                        "charged_seconds": 0,
                        "model": settings.openai_model,
                        "status": "completed",
                        "cache_hit": False,
                        "reused_entitlement": False,
                        "provider_cost_usd": 0,
                        "provider_cost_eur_estimate": 0,
                    },
                )
                snapshot = await _safe_snapshot(database, user.id, settings)
                return _analysis_response(
                    payload=payload,
                    items=[],
                    mode="ai",
                    billable_seconds=billable_seconds,
                    snapshot=snapshot,
                )

        result_items = (analysis or {}).get("result") or []
        if not result_items and analysis is not None:
            analysis_id = str(analysis["id"])
            await database.complete_entitlement(
                entitlement_id=entitlement_id,
                request_id=request_id,
                analysis_id=analysis_id,
                charged_seconds=0,
            )
            await _safe_usage_event(
                database,
                {
                    "user_id": user.id,
                    "request_id": request_id,
                    "video_id": payload.video_id,
                    "transcript_hash": transcript_hash,
                    "settings_hash": settings_hash,
                    "analysis_id": analysis_id,
                    "billing_mode": profile.get("billing_mode", "prepaid"),
                    "billable_seconds": 0,
                    "charged_seconds": 0,
                    "model": settings.openai_model,
                    "status": "completed",
                    "cache_hit": True,
                    "reused_entitlement": False,
                    "provider_cost_usd": 0,
                    "provider_cost_eur_estimate": 0,
                },
            )
            snapshot = await _safe_snapshot(database, user.id, settings)
            return _analysis_response(
                payload=payload,
                items=[],
                mode="ai",
                billable_seconds=billable_seconds,
                snapshot=snapshot,
                cache_hit=True,
            )

        billing_mode = str(profile.get("billing_mode") or "prepaid")
        if billing_mode == "prepaid":
            try:
                reservation_id = await database.reserve_credits(
                    user_id=user.id,
                    request_id=request_id,
                    video_id=payload.video_id,
                    transcript_hash=transcript_hash,
                    settings_hash=settings_hash,
                    required_seconds=billable_seconds,
                    ttl_minutes=settings.reservation_ttl_minutes,
                )
            except SupabaseDatabaseError as exc:
                if "insufficient_credits" in str(exc).lower():
                    raise api_error(
                        402,
                        "insufficient_credits",
                        "You do not have enough AI minutes for this video.",
                        required_seconds=billable_seconds,
                    ) from exc
                raise

        if analysis is None:
            snapshot_before = await _safe_snapshot(database, user.id, settings)
            if (
                billing_mode in {"at_cost", "free_admin"}
                and snapshot_before
                and snapshot_before.get("monthly_cost_limit_usd") is not None
                and float(snapshot_before.get("current_month_provider_cost_usd") or 0)
                >= float(snapshot_before["monthly_cost_limit_usd"])
            ):
                raise api_error(
                    402,
                    "monthly_cost_limit_reached",
                    "Your internal monthly OpenAI cost limit has been reached.",
                )

            ai_result: AITranslationResult = await get_ai_service().translate(candidates or [])
            usage = ai_result.usage
            result_items = vocab.expand_ai_results(
                candidates or [],
                ai_result.items,
                include_example=payload.options.include_example,
            )
            analysis_id = await database.upsert_analysis(
                {
                    "p_video_id": payload.video_id,
                    "p_transcript_hash": transcript_hash,
                    "p_settings_hash": settings_hash,
                    "p_source_language": payload.source_language,
                    "p_target_language": payload.options.target_language,
                    "p_prompt_version": settings.openai_prompt_version,
                    "p_model": settings.openai_model,
                    "p_result": result_items,
                    "p_input_tokens": usage.input_tokens,
                    "p_cached_input_tokens": usage.cached_input_tokens,
                    "p_output_tokens": usage.output_tokens,
                    "p_provider_cost_usd": float(usage.provider_cost_usd),
                }
            )
            canonical_analysis = await database.get_analysis(analysis_id)
            if canonical_analysis is not None and isinstance(
                canonical_analysis.get("result"), list
            ):
                # Concurrent users can generate the same cache key at nearly the
                # same time. Always return the canonical row selected by Postgres.
                result_items = canonical_analysis["result"]
        else:
            analysis_id = str(analysis["id"])

        charged_seconds = billable_seconds if billing_mode == "prepaid" else 0
        if billing_mode == "prepaid":
            if not reservation_id:
                raise RuntimeError("Credit reservation was not created.")
            await database.finalize_purchase(
                reservation_id=reservation_id,
                entitlement_id=entitlement_id,
                request_id=request_id,
                analysis_id=analysis_id,
                charged_seconds=charged_seconds,
            )
            reservation_id = None
        else:
            await database.complete_entitlement(
                entitlement_id=entitlement_id,
                request_id=request_id,
                analysis_id=analysis_id,
                charged_seconds=0,
            )

        provider_cost_eur = usage.provider_cost_usd * Decimal(str(settings.openai_usd_to_eur_rate))
        await _safe_usage_event(
            database,
            {
                "user_id": user.id,
                "request_id": request_id,
                "video_id": payload.video_id,
                "transcript_hash": transcript_hash,
                "settings_hash": settings_hash,
                "analysis_id": analysis_id,
                "billing_mode": billing_mode,
                "billable_seconds": billable_seconds,
                "charged_seconds": charged_seconds,
                "model": settings.openai_model,
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "output_tokens": usage.output_tokens,
                "provider_cost_usd": float(usage.provider_cost_usd),
                "provider_cost_eur_estimate": float(provider_cost_eur),
                "pricing_snapshot": {
                    "input_usd_per_million": settings.openai_input_usd_per_million,
                    "cached_input_usd_per_million": settings.openai_cached_input_usd_per_million,
                    "output_usd_per_million": settings.openai_output_usd_per_million,
                    "usd_to_eur_rate": settings.openai_usd_to_eur_rate,
                },
                "status": "completed",
                "cache_hit": cache_hit,
                "reused_entitlement": False,
            },
        )
        snapshot = await _safe_snapshot(database, user.id, settings)
        return _analysis_response(
            payload=payload,
            items=result_items,
            mode="ai",
            billable_seconds=billable_seconds,
            charged_seconds=charged_seconds,
            snapshot=snapshot,
            cache_hit=cache_hit,
            provider_cost_usd=usage.provider_cost_usd,
            provider_cost_eur=provider_cost_eur,
        )

    except Exception as exc:
        if isinstance(exc, AITranslationError):
            usage = exc.usage
        if reservation_id:
            try:
                await database.release_reservation(reservation_id)
            except Exception:
                logger.exception("Could not release reservation %s", reservation_id)
        try:
            await database.fail_entitlement(entitlement_id, request_id)
        except Exception:
            logger.exception("Could not mark entitlement %s as failed", entitlement_id)

        await _safe_usage_event(
            database,
            {
                "user_id": user.id,
                "request_id": request_id,
                "video_id": payload.video_id,
                "transcript_hash": transcript_hash,
                "settings_hash": settings_hash,
                "analysis_id": analysis_id,
                "billing_mode": profile.get("billing_mode", "prepaid"),
                "billable_seconds": billable_seconds,
                "charged_seconds": 0,
                "model": settings.openai_model,
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "output_tokens": usage.output_tokens,
                "provider_cost_usd": float(usage.provider_cost_usd),
                "provider_cost_eur_estimate": float(
                    usage.provider_cost_usd * Decimal(str(settings.openai_usd_to_eur_rate))
                ),
                "status": "failed",
                "error_message": str(exc)[:2000],
                "cache_hit": cache_hit,
                "reused_entitlement": False,
            },
        )

        if hasattr(exc, "status_code"):
            raise
        if isinstance(exc, SupabaseDatabaseError):
            logger.exception("Supabase database error")
            raise api_error(503, "database_error", "The billing database is unavailable.") from exc
        logger.exception("AI analysis failed")
        raise api_error(
            502,
            "ai_analysis_failed",
            "AI analysis failed. No prepaid minutes were charged.",
        ) from exc
