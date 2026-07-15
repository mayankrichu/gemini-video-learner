from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from .auth import close_auth_verifier
from .config import get_settings
from .database import close_database
from .rate_limit import limiter
from .routers import account, analysis, billing
from .vocab import get_vocab_service

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s %s", settings.app_name, settings.app_version)
    try:
        vocab = await asyncio.to_thread(get_vocab_service)
        logger.info("Dictionary sizes: %s", vocab.dictionary_sizes())
    except Exception:
        logger.exception("Vocabulary service failed to initialize")
        raise
    yield
    await close_auth_verifier()
    await close_database()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)
app.state.limiter = limiter

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_origin_regex=settings.allowed_origin_regex or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Stripe-Signature"],
)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": {
                "code": "rate_limit_exceeded",
                "message": "Rate limit exceeded. Try again shortly.",
            }
        },
    )


@app.get("/")
async def root() -> dict:
    return {
        "status": "online",
        "service": settings.app_name,
        "version": settings.app_version,
        "free_endpoint": "/v1/analyze/free",
        "ai_endpoint": "/v1/analyze/ai",
    }


@app.get("/health")
async def health() -> dict:
    return {
        "status": "online",
        "environment": settings.environment,
        "supabase_configured": bool(
            settings.supabase_url
            and settings.supabase_publishable_key
            and settings.supabase_secret_key
        ),
        "openai_configured": bool(settings.openai_api_key),
        "stripe_configured": bool(settings.stripe_secret_key and settings.stripe_webhook_secret),
    }


app.include_router(analysis.router)
app.include_router(account.router)
app.include_router(billing.router)
