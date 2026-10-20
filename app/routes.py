"""
SaveIt Backend - API route handlers.

Defines the three API endpoints specified in the PRD:
1. GET  /api/v1/health     — Health check
2. POST /api/v1/resolve    — Metadata + format extraction
3. GET  /api/v1/download   — Media download (merge/proxy streaming)

All endpoints are stateless. Rate limiting is applied via slowapi.
Metadata results are cached in-memory with a TTL.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from cachetools import TTLCache
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings
from app.extractor import (
    AgeRestrictedError,
    ContentNotFoundError,
    ContentPrivateError,
    DurationTooLongError,
    ExtractionError,
    _get_cookie_file,
    cleanup_old_temp_files,
    download_media,
    extract_metadata,
)
from app.schemas import (
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    Platform,
    ResolveRequest,
    ResolveResponse,
)
from app.token_utils import verify_download_token
from app.url_utils import clean_url, validate_url

logger = logging.getLogger("saveit.routes")

# ──────────────────────────────────────────────
# Router & Rate Limiter
# ──────────────────────────────────────────────

router = APIRouter(prefix="/api/v1", tags=["SaveIt API v1"])

settings = get_settings()
limiter = Limiter(key_func=get_remote_address)

# In-memory metadata cache (no DB, no Redis)
_metadata_cache: TTLCache = TTLCache(
    maxsize=settings.metadata_cache_max_size,
    ttl=settings.metadata_cache_ttl_seconds,
)

# ──────────────────────────────────────────────
# Error Response Builder
# ──────────────────────────────────────────────

def _error_response(code: ErrorCode, message: str, status_code: int) -> JSONResponse:
    """Build a standardized error JSON response."""
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump())


# ──────────────────────────────────────────────
# 1. Health Check
# ──────────────────────────────────────────────

@router.get("/health")
async def health_check():
    """Health check endpoint with cookie diagnostic."""
    import http.cookiejar
    cookie_path = _get_cookie_file()
    has_cookies = False
    cookie_bytes = 0
    cookie_count = 0
    if cookie_path and os.path.exists(cookie_path):
        has_cookies = True
        try:
            cookie_bytes = os.path.getsize(cookie_path)
            cj = http.cookiejar.MozillaCookieJar(cookie_path)
            cj.load(ignore_discard=True, ignore_expires=True)
            cookie_count = len(list(cj))
        except Exception:
            pass

    return {
        "status": "ok",
        "version": settings.api_version,
        "has_youtube_cookies": has_cookies,
        "cookie_source": cookie_path if has_cookies else None,
        "cookie_bytes": cookie_bytes,
        "cookie_count": cookie_count,
    }



# ──────────────────────────────────────────────
# 2. Resolve (Metadata + Formats)
# ──────────────────────────────────────────────

@router.post("/resolve", response_model=ResolveResponse)
@limiter.limit(settings.rate_limit)
async def resolve(request: Request, body: ResolveRequest):
    """
    Extract metadata and available download formats from a supported URL.

    The response includes a download_token that must be passed to the
    /download endpoint for merge/proxy downloads.
    """
    raw_url = body.url

    # Validate URL
    is_valid, result, platform = validate_url(raw_url)

    if not is_valid:
        error_code = (
            ErrorCode.UNSUPPORTED_PLATFORM
            if platform == Platform.UNKNOWN
            else ErrorCode.INVALID_URL
        )
        return _error_response(error_code, result, 400)

    cleaned_url = result

    # Check cache
    cache_key = cleaned_url
    if cache_key in _metadata_cache:
        logger.info(f"Cache hit for {cleaned_url}")
        return _metadata_cache[cache_key]

    # Extract metadata with timeout
    try:
        response = await asyncio.wait_for(
            extract_metadata(cleaned_url, platform),
            timeout=45,
        )
    except asyncio.TimeoutError:
        return _error_response(
            ErrorCode.FETCH_TIMEOUT,
            "Request timed out. Please try again.",
            504,
        )
    except ContentPrivateError as e:
        return _error_response(ErrorCode.CONTENT_PRIVATE, str(e), 403)
    except ContentNotFoundError as e:
        return _error_response(ErrorCode.CONTENT_NOT_FOUND, str(e), 404)
    except AgeRestrictedError as e:
        return _error_response(ErrorCode.AGE_RESTRICTED, str(e), 403)
    except DurationTooLongError as e:
        return _error_response(ErrorCode.FILE_TOO_LARGE, str(e), 400)
    except ExtractionError as e:
        return _error_response(ErrorCode.EXTRACTION_FAILED, str(e), 502)
    except Exception as e:
        logger.exception(f"Unexpected error resolving {cleaned_url}")
        return _error_response(
            ErrorCode.SERVER_ERROR,
            "An unexpected error occurred.",
            500,
        )

    # Cache the response
    _metadata_cache[cache_key] = response

    return response


# ──────────────────────────────────────────────
# 3. Download (Merge / Proxy Streaming)
# ──────────────────────────────────────────────

@router.get("/download")
@limiter.limit(settings.rate_limit)
async def download(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="The original content URL"),
    format_id: str = Query(..., description="The format ID to download"),
    item_id: str = Query("0", description="Item index (for carousels)"),
    token: str = Query(..., description="Signed download token"),
):
    """
    Download media for the given URL and format.

    For merge formats (1080p+), the backend downloads video and audio
    separately, merges them with FFmpeg, and streams the result.
    For direct formats, the backend acts as a proxy stream.

    The response is a binary file stream with appropriate headers.
    """
    # Verify token
    if not verify_download_token(token, url, format_id, item_id):
        return _error_response(
            ErrorCode.INVALID_URL,
            "Invalid or expired download token. Please fetch the link again.",
            403,
        )

    try:
        cleaned_url = clean_url(url)
        file_path, suggested_filename, content_length = await asyncio.wait_for(
            download_media(cleaned_url, format_id, item_id),
            timeout=300,  # 5 minute timeout for download+merge
        )
    except asyncio.TimeoutError:
        return _error_response(
            ErrorCode.FETCH_TIMEOUT,
            "Download timed out. Please try again.",
            504,
        )
    except ContentPrivateError as e:
        return _error_response(ErrorCode.CONTENT_PRIVATE, str(e), 403)
    except ContentNotFoundError as e:
        return _error_response(ErrorCode.CONTENT_NOT_FOUND, str(e), 404)
    except ExtractionError as e:
        return _error_response(ErrorCode.EXTRACTION_FAILED, str(e), 502)
    except Exception as e:
        logger.exception(f"Download failed for {url}")
        return _error_response(
            ErrorCode.SERVER_ERROR,
            "An unexpected error occurred during download.",
            500,
        )

    # Determine content type
    ext = file_path.suffix.lower()
    content_type_map = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    media_type = content_type_map.get(ext, "application/octet-stream")

    # Schedule cleanup of the temp file after streaming
    parent_dir = file_path.parent
    background_tasks.add_task(_cleanup_path, parent_dir)

    # Build response headers with RFC 5987 compliant encoding (handles emojis/Unicode)
    ascii_clean = "".join(c for c in suggested_filename if c.isalnum() or c in " ._-").strip()
    if not Path(ascii_clean).suffix and file_path.suffix:
        ascii_clean = f"{ascii_clean}{file_path.suffix}"
    if not ascii_clean:
        ascii_clean = f"media{file_path.suffix}"

    encoded_filename = quote(suggested_filename, encoding="utf-8")
    headers = {
        "Content-Disposition": f'attachment; filename="{ascii_clean}"; filename*=UTF-8\'\'{encoded_filename}',
    }
    if content_length:
        headers["Content-Length"] = str(content_length)

    # Stream the file
    return StreamingResponse(
        _file_streamer(file_path),
        media_type=media_type,
        headers=headers,
    )


async def _file_streamer(file_path: Path, chunk_size: int = 1024 * 1024):
    """Stream a file in chunks (1 MB default)."""
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk


def _cleanup_path(path: Path):
    """Remove a file or directory tree."""
    import shutil
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Failed to cleanup {path}: {e}")
