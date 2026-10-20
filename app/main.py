"""
SaveIt Backend - FastAPI Application Entry Point.

Creates and configures the FastAPI application with:
- CORS middleware
- Rate limiting (slowapi)
- Periodic temp file cleanup (background task)
- API routes
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.extractor import cleanup_old_temp_files
from app.routes import limiter, router
from app.schemas import ErrorCode, ErrorDetail, ErrorResponse

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("saveit")


# ──────────────────────────────────────────────
# Background Cleanup Task
# ──────────────────────────────────────────────

async def _periodic_cleanup():
    """Periodically clean up old temporary files."""
    settings = get_settings()
    interval = settings.temp_cleanup_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            cleanup_old_temp_files()
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")


# ──────────────────────────────────────────────
# Application Lifespan
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown events."""
    settings = get_settings()
    logger.info(f"SaveIt Backend v{settings.api_version} starting...")
    logger.info(f"Debug mode: {settings.debug}")

    # Start periodic cleanup
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    logger.info(
        f"Temp cleanup scheduled every {settings.temp_cleanup_interval_seconds}s "
        f"(max age: {settings.temp_max_age_seconds}s)"
    )

    yield

    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("SaveIt Backend shutting down.")


# ──────────────────────────────────────────────
# Create Application
# ──────────────────────────────────────────────

def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="SaveIt Backend",
        description="Link se Video & Image Downloader – Stateless Backend API",
        version=settings.api_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )

    # ── CORS ──
    origins = settings.allowed_origins
    if origins == "*":
        allow_origins = ["*"]
    else:
        allow_origins = [o.strip() for o in origins.split(",")]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate Limiter ──
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Custom 429 handler ──
    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        body = ErrorResponse(
            error=ErrorDetail(
                code=ErrorCode.RATE_LIMITED,
                message="Too many requests. Please try again later.",
            )
        )
        return JSONResponse(status_code=429, content=body.model_dump())

    # ── Global Exception Handler ──
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception(f"Unhandled exception: {exc}")
        body = ErrorResponse(
            error=ErrorDetail(
                code=ErrorCode.SERVER_ERROR,
                message="An internal error occurred.",
            )
        )
        return JSONResponse(status_code=500, content=body.model_dump())

    # ── Root Endpoint ──
    @app.get("/")
    async def root():
        return {
            "message": "SaveIt Backend API is Live & Running!",
            "status": "healthy",
            "version": settings.api_version,
            "endpoints": {
                "health": "/api/v1/health",
                "resolve": "/api/v1/resolve",
                "download": "/api/v1/download",
            },
        }

    # ── Routes ──
    app.include_router(router)

    return app



# The application instance (used by uvicorn)
app = create_app()
